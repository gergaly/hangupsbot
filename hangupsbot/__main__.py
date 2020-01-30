#!/usr/bin/env python

# Install modern gettext class-based API in Python's builtins namespace first
import os, io, gettext
localedir = os.path.join(os.path.dirname(__file__), 'locale')
gettext.install('hangupsbot', localedir=localedir)

# For argparse localization to work, we need also to setup old GNU gettext API
gettext.bindtextdomain('hangupsbot', localedir=localedir)
gettext.textdomain('hangupsbot')

import sys, argparse, logging, shutil, asyncio, time, signal

import appdirs
import hangups
from hangups import http_utils
from hangups.conversation import Conversation
from hangups.ui.utils import get_conv_name

import hangupsbot.config
from hangupsbot.version import __version__
from hangupsbot.utils import text_to_segments
from hangupsbot.handlers import handler

import socket
import uuid
import paho.mqtt.client as mqtt
import asyncio

LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

logger = logging.getLogger(__name__).setLevel(logging.INFO)


class AsyncioHelper:
    def __init__(self, loop, client, logger):
        self.loop = loop
        self.client = client
        self.client.on_socket_open = self.on_socket_open
        self.client.on_socket_close = self.on_socket_close
        self.client.on_socket_register_write = self.on_socket_register_write
        self.client.on_socket_unregister_write = self.on_socket_unregister_write
        self.logger = logger

    def on_socket_open(self, client, userdata, sock):

        def cb():
            client.loop_read()

        self.loop.add_reader(sock, cb)
        self.misc = self.loop.create_task(self.misc_loop())

    def on_socket_close(self, client, userdata, sock):
        self.loop.remove_reader(sock)
        self.misc.cancel()

    def on_socket_register_write(self, client, userdata, sock):

        def cb():
            client.loop_write()

        self.loop.add_writer(sock, cb)

    def on_socket_unregister_write(self, client, userdata, sock):
        self.loop.remove_writer(sock)

    async def misc_loop(self):
        while self.client.loop_misc() == mqtt.MQTT_ERR_SUCCESS:
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break

class AsyncMqttExample:
    def __init__(self, loop, bot, logger):
        self.loop = loop
        self.bot = bot
        self.logger = logger
        self.topic_dict = {}
        self.conv = None

    def on_subscribe(self, client, userdata, mid, granted_qos):
        if mid in self.topic_dict:
            self.logger.info('Subscribed to %s',self.topic_dict[mid])
        else:
            self.logger.warning('Subscribed to unknown topic, mid: %s',mid)

    def on_connect(self, client, userdata, flags, rc):
        self.logger.info('Connected to %s',self.bot.get_config_mqtt('server'))
        for t in self.bot.get_config_mqtt('topics'):
            if t:
                res = client.subscribe(t)
                if res[0] == mqtt.MQTT_ERR_SUCCESS:
                    self.topic_dict[res[1]] = t

    def on_message(self, client, userdata, msg):
        if self.conv == None:
            cid = self.bot.get_config_mqtt('conversation')
            convs = self.bot.find_conversations(cid)
            self.conv = convs[0]
        self.bot.send_message(self.conv,str(msg.topic)+" "+str(msg.payload,'utf-8'))

    def on_disconnect(self, client, userdata, rc):
        self.disconnected.set_result(rc)

    async def disconnect(self):
        self.logger.info('Disconnecting from %s',self.bot.get_config_mqtt('server'))
        self.client.disconnect()

    async def main(self,bot):
        self.disconnected = self.loop.create_future()
        self.got_message = None
        self.bot = bot

        client_id = self.bot.get_config_mqtt('client_id')
        self.client = mqtt.Client(client_id=client_id)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.on_subscribe = self.on_subscribe
        self.client.on_disconnect = self.on_disconnect

        aioh = AsyncioHelper(self.loop, self.client, self.logger)

        server = self.bot.get_config_mqtt('server')
        self.client.connect(server, 1883, 60)
        self.client.socket().setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 2048)

class HangupsBot:
    """Hangouts bot listening on all conversations"""
    def __init__(self, loop, refresh_token_path, config_path, logger, max_retries=5):
        self._client = None
        self._refresh_token_path = refresh_token_path
        self._max_retries = max_retries
        self._retry = 0
        self.loop = loop
        self.logger = logger

        # These are populated by on_connect when it's called.
        self._conv_list = None        # hangups.ConversationList
        self._user_list = None        # hangups.UserList

        # Load config file
        self.config = hangupsbot.config.Config(config_path)

        # Handle signals on Unix
        # (add_signal_handler is not implemented on Windows)
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                self.loop.add_signal_handler(signum, lambda: self.sig())
        except NotImplementedError:
            pass

    def login(self,ame):
        """Login to Google account"""
        # Authenticate Google user with OAuth token and save it
        # (or load already saved OAuth token)
        self.ame = ame
        try:
            cookies = hangups.auth.get_auth_stdin(self._refresh_token_path)
            if cookies:
                self._client = hangups.Client(cookies)
                self._client.on_connect.add_observer(self._on_connect)
                self._client.on_disconnect.add_observer(self._on_disconnect)
        except hangups.GoogleAuthError as e:
            print(_('Login failed ({})').format(e))
            return False

    def sig(self):
        asyncio.gather(
            self.disconnect(),
            self.ame.disconnect()
        ).add_done_callback(self.stop)

    def stop(self,arg):
        self.logger.info('Exiting')

    async def disconnect(self):
        self.logger.info('Disconnecting from Hangouts')
        await self._client.disconnect()

    def send_message(self, conversation, text):
        """Send simple chat message"""
        self.send_message_segments(conversation, text_to_segments(text))

    def send_message_segments(self, conversation, segments):
        """Send chat message segments"""
        # Ignore if the user hasn't typed a message.
        if len(segments) == 0:
            return
        # XXX: Exception handling here is still a bit broken. Uncaught
        # exceptions in _on_message_sent will only be logged.
        asyncio.async(
            conversation.send_message(segments)
        ).add_done_callback(self._on_message_sent)

    @asyncio.coroutine
    def upload_images(self, links):
        """Download images and upload them to Google+"""
        image_id_list = []
        for link in links:
            # Download image
            try:
                res = yield from http_utils.fetch('get', link)
            except hangups.NetworkError as e:
                print('Failed to download image: {}'.format(e))
                continue

            # Upload image and get image_id
            try:
                image_id = yield from self._client.upload_image(io.BytesIO(res.body),
                                                                filename=os.path.basename(link))
                image_id_list.append(image_id)
            except hangups.NetworkError as e:
                print('Failed to upload image: {}'.format(e))
                continue
        return image_id_list

    def list_conversations(self):
        """List all active conversations"""
        convs = sorted(self._conv_list.get_all(),
                       reverse=True, key=lambda c: c.last_modified)
        return convs

    def find_conversations(self, conv_name):
        """Find conversations by name or ID in list of all active conversations"""
        conv_name = conv_name.strip()
        conv_name_lower = conv_name.lower()
        if conv_name_lower.startswith("id:"):
            return [self._conv_list.get(conv_name[3:])]

        convs = [c for c in self.list_conversations()
                 if conv_name_lower in get_conv_name(c, truncate=True).lower()]
        return convs

    def list_users(self, conv=None):
        """List all known users or all users in conversation"""
        def full_name_sort(user):
            split_name = user.full_name.split()
            return (split_name[-1], split_name[0])
        users = conv.users if isinstance(conv, Conversation) else self._user_list.get_all()
        return sorted(users, key=full_name_sort)

    def find_users(self, user_name, conv=None):
        """Find users by name or ID in list of all known users or in conversation"""
        user_name = user_name.strip()
        user_name_lower = user_name.lower()
        if user_name_lower.startswith("id:"):
            return [self._user_list.get_user(user_name[3:])]

        users = [u for u in self.list_users(conv=conv)
                 if user_name_lower in u.full_name.lower()]
        return users

    def get_config_mqtt(self, key):
        try:
            option = self.config['mqtt'][key]
        except KeyError:
            option = None
        return option

    def get_config_suboption(self, conv_id, option):
        """Get config suboption for conversation (or global option if not defined)"""
        try:
            suboption = self.config['conversations'][conv_id][option]
        except KeyError:
            try:
                suboption = self.config[option]
            except KeyError:
                suboption = None
        return suboption

    def _on_message_sent(self, future):
        """Handle showing an error if a message fails to send"""
        try:
            future.result()
        except hangups.NetworkError:
            print(_('Failed to send message!'))

    @asyncio.coroutine
    def _on_connect(self):
        """Handle connecting for the first time"""
        self.logger.info('Connected to Hangouts')
        self._retry = 0
        self._user_list, self._conv_list = (
            yield from hangups.build_user_conversation_list(self._client)
        )
        self._conv_list.on_event.add_observer(self._on_event)

    @asyncio.coroutine
    def _on_event(self, conv_event):
        """Handle conversation events"""
        yield from handler.handle(self, conv_event)

    @asyncio.coroutine
    def _on_disconnect(self):
        """Handle disconnecting"""
        print(_('Connection lost!'))


def main():
    """Main entry point"""
    # Build default paths for files.
    dirs = appdirs.AppDirs('hangupsbot', 'hangupsbot')
    default_log_path = os.path.join(dirs.user_data_dir, 'hangupsbot.log')
    default_token_path = os.path.join(dirs.user_data_dir, 'refresh_token.txt')
    default_config_path = os.path.join(dirs.user_data_dir, 'config.json')

    # Configure argument parser
    parser = argparse.ArgumentParser(prog='hangupsbot',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-d', '--debug', action='store_true',
                        help=_('log detailed debugging messages'))
    parser.add_argument('--log', default=default_log_path,
                        help=_('log file path'))
    parser.add_argument('--token', default=default_token_path,
                        help=_('OAuth refresh token storage path'))
    parser.add_argument('--config', default=default_config_path,
                        help=_('config storage path'))
    parser.add_argument('--version', action='version', version='%(prog)s {}'.format(__version__),
                        help=_('show program\'s version number and exit'))
    args = parser.parse_args()

    # Create all necessary directories.
    for path in [args.log, args.token, args.config]:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            try:
                os.makedirs(directory)
            except OSError as e:
                sys.exit(_('Failed to create directory: {}').format(e))

    # If there is no config file in user data directory, copy default one there
    if not os.path.isfile(args.config):
        try:
            shutil.copy(os.path.abspath(os.path.join(os.path.dirname(__file__), 'config.json')),
                        args.config)
        except (OSError, IOError) as e:
            sys.exit(_('Failed to copy default config file: {}').format(e))

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(filename=args.log, level=log_level, format=LOG_FORMAT)
    # asyncio's debugging logs are VERY noisy, so adjust the log level
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logger = logging.getLogger('main')
    logger.setLevel(logging.INFO)

    loop = asyncio.get_event_loop()
    # Start Hangups bot
    bot = HangupsBot(loop, args.token, args.config, logger)
    # Start mqtt client
    ame = AsyncMqttExample(loop,bot, logger)
    bot.login(ame)
    # Start both tasks
    all_tasks = asyncio.gather(bot._client.connect(),ame.main(bot))
    loop.run_until_complete(all_tasks)
    loop.close()


if __name__ == '__main__':
    main()
