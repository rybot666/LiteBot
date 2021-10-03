import asyncio
from datetime import datetime
from typing import Callable, Union

import discord
import os

import mongoengine
from discord.ext import commands
from discord.ext.commands import Command
from sanic import Sanic
from sanic.log import logger, access_logger
from sanic_cors import CORS

from litebot.core.context import Context
from litebot.core.cog import Cog
from litebot.core.minecraft.commands.action import ServerCommand
from litebot.core.plugins import PluginManager, Plugin
from litebot.core.settings import SettingsManager
from litebot.utils.tracking_model import TrackedEvent
from litebot.server import APP_NAME, SERVER_HOST, SERVER_PORT, add_routes
from litebot.utils.config import MainConfig
from litebot.utils.logging import get_logger, set_logger, set_access_logger
from litebot.core.minecraft.server import MinecraftServer, ServerContainer

class GroupMixin(commands.GroupMixin):
    def __init__(self):
        self.server_commands: dict[str, ServerCommand] = {}
        self.server_events: dict[str, list[Callable]] = {}
        self.rpc_handlers: dict[str, Callable] = {}

    def add_command(self, command: Union[ServerCommand, Command]):
        """Add a command

        Args:
            command: The command to add
        """
        if not isinstance(command, ServerCommand):
            return super().add_command(command)

        self.server_commands[command.full_name] = command

    def remove_command(self, name):
        """Remove a command

        Args:
            name: The name of the command to remove
        """
        if name not in self.server_commands:
            return super().remove_command(name)

        self.server_commands.pop(name)

    def add_rpc_handler(self, handler, name):
        """Add an RPC method

        Args:
            handler: The handler for the RPC method
            name: The name of the handler
        """

        self.rpc_handlers[name] = handler

    def remove_rpc_handler(self, name):
        """Remove an RPC method

        Args:
            name: The name of method to remove
        """
        del self.rpc_handlers[name]

    def add_server_listener(self, func, name):
        """Add a server listener

        Args:
            func: The listener for the event
            name: The name of the handler
        """
        if name in self.server_events:
            return self.server_events[name].append(func)

        self.server_events[name] = [func]

    def remove_server_listener(self, func, name):
        """Remove a server listener

        Args:
            func: The listener to remove
            name: The name of the listner
        """
        self.server_events[name] = list(filter(lambda e: e is not func, self.server_events[name]))

class LiteBot(GroupMixin, commands.Bot):
    VERSION = "3.0.1"

    def __init__(self):
        self.config = MainConfig()
        self.settings_manager = SettingsManager()
        self.plugin_manager = PluginManager(self)

        commands.Bot.__init__(
            self,
            command_prefix=commands.when_mentioned_or(*self.config["prefixes"]),
            help_command=None,
            intents=discord.Intents.all(),
            case_insensitive=True)
        GroupMixin.__init__(self)
        self.logger = get_logger("bot")

        self.db = mongoengine.connect(os.environ.get('DB_NAME'), host=os.environ.get('DB_HOST'), port=int(os.environ.get('DB_PORT')))
        self.logger.info("Connected to Mongo Database")

        self.processing_plugin = None

        self.using_lta = bool(os.environ.get("USING_LTA"))
        self.__server = Sanic(APP_NAME)
        self.servers = self._init_servers()
        self.loop.create_task(asyncio.to_thread(self._dispatch_timers))

    @property
    def log_channel(self) -> discord.TextChannel:
        """
        Returns:
            The log channel for the bot
        """
        return self.get_channel(self.config["log_channel_id"])

    @property
    def server(self):
        """
        Returns:
            The sanic server
        """
        return self.__server

    async def guild(self) -> discord.Guild:
        """
        Returns:
            The main guild object for the server
        """
        await self.wait_until_ready()
        return self.get_guild(self.config["main_guild_id"])

    async def get_context(self, message, *, cls=Context):
        return await super().get_context(message, cls=cls)

    def add_cog(self, cog, *args, **kwargs):
        if not issubclass(cog, Cog):
            raise TypeError("cogs must be a subclass of Cog!")

        try:
            cog = cog(*(*args, self, self.processing_plugin), **kwargs)
        except TypeError:
            cog = cog(*args, **kwargs)

        super().add_cog(cog)

    def load_plugin(self, plugin: Plugin):
        """Load a plugin

        Args:
            plugin: The plugin to load
        """
        self.processing_plugin = plugin
        super().load_extension(plugin.path)

    def unload_plugin(self, plugin):
        """Unload a plugin

        Args:
            plugin: The plugin to unload
        """
        self.processing_plugin = plugin
        super().unload_extension(plugin.path)

    async def on_ready(self):
        """
        on_ready logger
        """
        self.logger.info(f"{self.user.name} is now online!")

    def start_server(self):
        """Start the sanic server
        """

        CORS(self.__server)

        # A stupid hackfix that I have to do to make the logging work appropriately
        # I don't like it, but I don't see a better way to achieve this
        # Personally I think this is cleaner then using the dictConfig
        set_logger(logger)
        set_access_logger(access_logger)

        self.__server.config.FALLBACK_ERROR_FORMAT = "json"
        self.__server.config.BOT_INSTANCE = self
        # This is set so that we can properly generate URLs to our server
        self.config.SERVER_NAME = os.environ.get("SERVER_NAME")

        add_routes(self.__server)

        coro = self.__server.create_server(host=SERVER_HOST, port=SERVER_PORT, return_asyncio_server=True,
                                          access_log=False)
        self.loop.create_task(coro)

    def _init_servers(self):
        container = ServerContainer()
        for server in self.config["servers"]:
            container.append(MinecraftServer(server, self, **self.config["servers"][server]))
        return container

    def _schedule_event(self, coro, event_name, *args, **kwargs):
        if hasattr(coro, "__setting__"):
            args = (coro.__setting__, *args)
        wrapped = self._run_event(coro, event_name, *args, **kwargs)
        return self.loop.create_task(wrapped, name=f"discord.py: {event_name}")

    def _dispatch_timers(self):
        while True:
            events = TrackedEvent.objects()
            for event in events:
                if not event.expire_time:
                    continue

                if datetime.utcnow() >= event.expire_time:
                    name = f"{event.event_tag}_expire"
                    self.dispatch(name, event)

                    event.delete()

