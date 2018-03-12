import asyncio
import inspect
import logging
import math
import os
import pathlib
import random
import re
import shlex
import shutil
import sys
import time
import traceback
#########
##custom
import urllib.parse
from collections import defaultdict
from datetime import timedelta
from functools import wraps
##
########
from io import BytesIO, StringIO
from textwrap import dedent

import aiohttp
import colorlog
import discord
from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable

from . import downloader, exceptions
from .config import Config, ConfigDefaults
from .constants import VERSION as BOTVERSION
from .constants import AUDIO_CACHE_PATH, DISCORD_MSG_CHAR_LIMIT
from .constructs import Response, SkipState, VoiceStateUpdate
from .entry import StreamPlaylistEntry
from .opus_loader import load_opus_lib
from .player import MusicPlayer
from .playlist import Playlist
from .utils import _func_, fixg, ftimedelta, load_file, write_file

load_opus_lib()

log = logging.getLogger(__name__)


class MusicBot(discord.Client):
    def __init__(self, config_file=None, perms_file=None):
        try:
            sys.stdout.write("\x1b]2;MusicBot {}\x07".format(BOTVERSION))
        except:
            pass

        if config_file is None:
            config_file = ConfigDefaults.options_file

        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None

        self.config = Config(config_file)

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.autoplaylist = load_file(self.config.auto_playlist_file)
        self.autoplaylist_session = self.autoplaylist[:]

        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        self._setup_logging()

        log.info(' MusicBot (version {}) '.format(BOTVERSION).center(50, '='))

        if not self.autoplaylist:
            log.warning("자동재생목록이 비어있다네. 비활성화 하겠네.")
            self.config.auto_playlist = False
        else:
            log.info("자동재생목록으로부터 {} 개 음악을 불러오겠네".format(len(self.autoplaylist)))

        if self.blacklist:
            log.debug("추방목록으로부터 {} 명어치를 불러왔다네".format(len(self.blacklist)))

        # TODO: Do these properly
        ssd_defaults = {
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    def __del__(self):
        # These functions return futures but it doesn't matter
        try:    self.http.session.close()
        except: pass

        try:    self.aiosession.close()
        except: pass

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("주인만 이 명령어를 사용할수 있다네!", expire_in=30)

        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if orig_msg.author.id in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("개발자들만 이 명령어를 사용할수 있다네!", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
            return discord.utils.find(
                lambda m: m.id == self.config.owner_id and (m.voice_channel if voice else True),
                server.members if server else self.get_all_members()
            )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("로그 작성기 설정을 건너뛰겠네, 이미 작성된거같다네")
            return

        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt = {
                'DEBUG': '{log_color}[{levelname}:{module}] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:{module}] {message}',
                'CRITICAL': '{log_color}[{levelname}:{module}] {message}',

                'EVERYTHING': '{log_color}[{levelname}:{module}] {message}',
                'NOISY': '{log_color}[{levelname}:{module}] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}',
                'FFMPEG': '{log_color}[{levelname}:{module}][{relativeCreated:.9f}] {message}'
            },
            log_colors = {
                'DEBUG':    'cyan',
                'INFO':     'white',
                'WARNING':  'yellow',
                'ERROR':    'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY':      'white',
                'FFMPEG':     'bold_purple',
                'VOICEDEBUG': 'purple',
        },
            style = '{',
            datefmt = ''
        ))
        shandler.setLevel(self.config.debug_level)
        logging.getLogger(__package__).addHandler(shandler)

        log.debug("로그 레벨을 {}로 설정하겠네".format(self.config.debug_level_str))

        if self.config.debug_mode:
            dlogger = logging.getLogger('discord')
            dlogger.setLevel(logging.DEBUG)
            dhandler = logging.FileHandler(filename='logs/discord.log', encoding='utf-8', mode='w')
            dhandler.setFormatter(logging.Formatter('{asctime}:{levelname}:{name}: {message}', style='{'))
            dlogger.addHandler(dhandler)

    @staticmethod
    def _check_if_empty(vchannel: discord.Channel, *, excluding_me=True, excluding_deaf=False):
        def check(member):
            if excluding_me and member == vchannel.server.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        return not sum(1 for m in vchannel.voice_members if check(m))


    async def _join_startup_channels(self, channels, *, autosummon=True):
        joined_servers = set()
        channel_map = {c.server: c for c in channels}

        def _autopause(player):
            if self._check_if_empty(player.voice_client.channel):
                log.info("빈 채팅방에 들어가면 맨 처음에는 살짝 멈출거라네")

                player.pause()
                self.server_specific_data[player.voice_client.channel.server]['auto_paused'] = True

        for server in self.servers:
            if server.unavailable or server in channel_map:
                continue

            if server.me.voice_channel:
                log.info("돌아갈수 있는 음성채팅방을 찾은것 같다네 {0.server.name}/{0.name}".format(server.me.voice_channel))
                channel_map[server] = server.me.voice_channel

            if autosummon:
                owner = self._get_owner(server=server, voice=True)
                if owner:
                    log.info("주인을 \"{}\"에서 찾은것 같다네".format(owner.voice_channel.name))
                    channel_map[server] = owner.voice_channel

        for server, channel in channel_map.items():
            if server in joined_servers:
                log.info("이미 \"{}\" 채팅방에 들어갔다네, 건너뛰겠네".format(server.name))
                continue

            if channel and channel.type == discord.ChannelType.voice:
                log.info("{0.server.name}/{0.name} 채팅방에 참가 시도를 해보겠네".format(channel))

                chperms = channel.permissions_for(server.me)

                if not chperms.connect:
                    log.info(" \"{}\" 에 들어갈수 없는것 같다네. 권한이 없는것같다네.".format(channel.name))
                    continue

                elif not chperms.speak:
                    log.info("\"{}\"에 들어가지 않을 것이라네! 들어가서 말할수 있는 권한이 없다네!".format(channel.name))
                    continue

                try:
                    player = await self.get_player(channel, create=True, deserialize=self.config.persistent_queue)
                    joined_servers.add(server)

                    log.info("{0.server.name}/{0.name}에 참가했다네".format(channel))

                    if player.is_stopped:
                        player.play()

                    if self.config.auto_playlist and not player.playlist.entries:
                        await self.on_player_finished_playing(player)
                        if self.config.auto_pause:
                            player.once('play', lambda player, **_: _autopause(player))

                except Exception:
                    log.debug("{0.server.name}/{0.name}에 참가하는데 실패했다네.".format(channel), exc_info=True)
                    log.error("{0.server.name}/{0.name}에 참가하는데 실패했다네.".format(channel))

            elif channel:
                log.warning("{0.server.name}/{0.name}에 참가하지 않을거라네. 음성채팅방이 아닌것같네".format(channel))

            else:
                log.warning("올바른 채팅방이 아닌것같네: {}".format(channel))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "음성채팅방에 있지 않을때는 이 명령을 사용할수 없다네!(%s)" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info


    async def remove_from_autoplaylist(self, song_url:str, *, ex:Exception=None, delete_from_ap=False):
        if song_url not in self.autoplaylist:
            log.debug("\"{}\" 는 자동재생 목록에 없다네. 흘려듣겠네!".format(song_url))
            return

        async with self.aiolocks[_func_()]:
            self.autoplaylist.remove(song_url)
            log.info("재생을 할수없는 곡들을 자동재생 목록에서 삭제하고 있다네!: %s" % song_url)

            with open(self.config.auto_playlist_removed_file, 'a', encoding='utf8') as f:
                f.write(
                    '# 삭제된 항목 갯수 {ctime}\n'
                    '# 사유 갯수: {ex}\n'
                    '{url}\n\n{sep}\n\n'.format(
                        ctime=time.ctime(),
                        ex=str(ex).replace('\n', '\n#' + ' ' * 10), # 10 spaces to line up with # Reason:
                        url=song_url,
                        sep='#' * 32
                ))

            if delete_from_ap:
                log.info("자동재생 목록을 갱신중이라네!")
                write_file(self.config.auto_playlist_file, self.autoplaylist)

    @ensure_appinfo
    async def generate_invite_link(self, *, permissions=discord.Permissions(70380544), server=None):
        return discord.utils.oauth_url(self.cached_app_info.id, permissions=permissions, server=server)


    async def join_voice_channel(self, channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise discord.InvalidArgument('음성채팅방에만 들어갈수 있다네!')

        server = channel.server

        if self.is_voice_connected(server):
            raise discord.ClientException('이 서버의 음성채팅방에만 들어갈 수 있다네')

        def session_id_found(data):
            user_id = data.get('user_id')
            guild_id = data.get('guild_id')
            return user_id == self.user.id and guild_id == server.id

        log.voicedebug("(%s) 예약목록 만드는중.", _func_())
        # register the futures for waiting
        session_id_future = self.ws.wait_for('VOICE_STATE_UPDATE', session_id_found)
        voice_data_future = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: d.get('guild_id') == server.id)

        # "join" the voice channel
        log.voicedebug("(%s) 음량 조절 중.", _func_())
        await self.ws.voice_state(server.id, channel.id)

        log.voicedebug("(%s) 세션 아이디를 기다리는 중.", _func_())
        session_id_data = await asyncio.wait_for(session_id_future, timeout=15, loop=self.loop)

        # sometimes it gets stuck on this step.  Jake said to wait indefinitely.  To hell with that.
        log.voicedebug("(%s) 음성 데이터를 기다리는 중.", _func_())
        data = await asyncio.wait_for(voice_data_future, timeout=15, loop=self.loop)

        kwargs = {
            'user': self.user,
            'channel': channel,
            'data': data,
            'loop': self.loop,
            'session_id': session_id_data.get('session_id'),
            'main_ws': self.ws
        }

        voice = discord.VoiceClient(**kwargs)
        try:
            log.voicedebug("(%s) 연결중.", _func_())
            with aiohttp.Timeout(15):
                await voice.connect()

        except asyncio.TimeoutError as e:
            log.voicedebug("(%s) 연결에 실패했다네! 연결을 끊겠네", _func_())
            try:
                await voice.disconnect()
            except:
                pass
            raise e

        log.voicedebug("(%s) 연결 성공!", _func_())

        self.connection._add_voice_client(server.id, voice)
        return voice


    async def get_voice_client(self, channel: discord.Channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('음성 채널에만 들어갈수 있다네!')

        async with self.aiolocks[_func_() + ':' + channel.server.id]:
            if self.is_voice_connected(channel.server):
                return self.voice_client_in(channel.server)

            vc = None
            t0 = t1 = 0
            tries = 5

            for attempt in range(1, tries+1):
                log.debug("{} 에서 {}까지 연결 시도중.".format(attempt, channel.name))
                t0 = time.time()

                try:
                    vc = await self.join_voice_channel(channel)
                    t1 = time.time()
                    break

                except asyncio.TimeoutError:
                    log.warning("연결에 실패했다네. 재시도를 하는중이라네. ({}/{})".format(attempt, tries))

                    # TODO: figure out if I need this or not
                    # try:
                    #     await self.ws.voice_state(channel.server.id, None)
                    # except:
                    #     pass

                except:
                    log.exception("음악 모듈을 가져오는 시도중에 오류가 생겼다네.")

                await asyncio.sleep(0.5)

            if not vc:
                log.critical("음악 모듈을 가져올수 없었다네. 다시 시도중이라네.")
                await self.restart()

            log.debug("Connected in {:0.1f}s".format(t1-t0))
            log.info("Connected to {}/{}".format(channel.server, channel))

            vc.ws._keep_alive.name = '음악봇을 살려'

            return vc

    async def reconnect_voice_client(self, server, *, sleep=0.1, channel=None):
        log.debug("음악봇을 \"{}\"{}에 다시 접속 시도중이라네".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

        async with self.aiolocks[_func_() + ':' + server.id]:
            vc = self.voice_client_in(server)

            if not (vc or channel):
                return

            _paused = False
            player = self.get_player_in(server)

            if player and player.is_playing:
                log.voicedebug("(%s) 일시정지", _func_())

                player.pause()
                _paused = True

            log.voicedebug("(%s) 연결 해제중", _func_())

            try:
                await vc.disconnect()
            except:
                pass

            if sleep:
                log.voicedebug("(%s) %s동안 잠좀 자겠네", _func_(), sleep)
                await asyncio.sleep(sleep)

            if player:
                log.voicedebug("(%s) 음악 모듈을 가져오는중.", _func_())

                if not channel:
                    new_vc = await self.get_voice_client(vc.channel)
                else:
                    new_vc = await self.get_voice_client(channel)

                log.voicedebug("(%s) 음악 모듈을 교체중", _func_())
                await player.reload_voice(new_vc)

                if player.is_paused and _paused:
                    log.voicedebug("다시 시작중이라네")
                    player.resume()

        log.debug("\"{}\"{}에 있는 음악 모듈에 재접속했다네".format(
            server, ' 에서 "{}"'.format(channel.name) if channel else ''))

    async def disconnect_voice_client(self, server):
        vc = self.voice_client_in(server)
        if not vc:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.server)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('음성채팅방에만 들어갈 수 있다네!')

        await self.ws.voice_state(vchannel.server.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, server: discord.Server) -> MusicPlayer:
        return self.players.get(server.id)

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        server = channel.server

        async with self.aiolocks[_func_() + ':' + server.id]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(server, voice_client)

                if player:
                    log.debug("%s개 목록을 가지고 있는 %s 서버를 위해 비-직렬화를 통해서 사용자를 만들었다네!", len(player.playlist), server.id)
                    # Since deserializing only happens when the bot starts, I should never need to reconnect
                    return self._init_player(player, server=server)

            if server.id not in self.players:
                if not create:
                    raise exceptions.CommandError(
                        '이 음악봇은 음성채팅방에 들어가지 않은것 같다네. '
                        '%s 들어와 명령을 통해서 음성채팅방으로 불러주겠나' % self.config.command_prefix)

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, server=server)

            async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
                if self.players[server.id].voice_client not in self.voice_clients:
                    log.debug("{}에 있는 음악 모듈에 재접속이 필요할 것 같아보인다네.".format(server.name))
                    await self.reconnect_voice_client(server, channel=channel)

        return self.players[server.id]

    def _init_player(self, player, *, server=None):
        player = player.on('play', self.on_player_play) \
                       .on('resume', self.on_player_resume) \
                       .on('pause', self.on_player_pause) \
                       .on('stop', self.on_player_stop) \
                       .on('finished-playing', self.on_player_finished_playing) \
                       .on('entry-added', self.on_player_entry_added) \
                       .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if server:
            self.players[server.id] = player

        return player

    async def on_player_play(self, player, entry):

        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.server)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)
        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        #await self.cmd_clean(last_np_msg, channel, player.voice_client.channel.server, author)
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant
            #await self.cmd_queue(channel, player)
            
            if self.config.now_playing_mentions:
                newmsg = '%s - 요청한 곡인 **%s** 이 %s에서 재생중이라네!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = ' %s에서 재생중이라네!:\n **%s**' % (
                    player.voice_client.channel.name, entry.title)

            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)
            await self.cmd_clean(self.server_specific_data[channel.server]['last_np_msg'], channel, player.voice_client.channel.server, author)
            await self.cmd_queue(channel, player)

        # TODO: Check channel voice state?

    async def on_player_resume(self, player, entry, **_):
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        await self.update_now_playing_status(entry, True)
        # await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_stop(self, player, **_):
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry and self.config.auto_playlist:
            if not self.autoplaylist_session:
                log.info("자동재생 목록이 비어있다네! 재생목록에서 불러오는 중이라네.")
                self.autoplaylist_session = self.autoplaylist[:]

            while self.autoplaylist_session:
                random.shuffle(self.autoplaylist_session)
                song_url = random.choice(self.autoplaylist_session)
                self.autoplaylist_session.remove(song_url)

                info = {}

                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                except downloader.youtube_dl.utils.DownloadError as e:
                    if 'YouTube said:' in e.args[0]:
                        # url is bork, remove from list and put in removed list
                        log.error("유튜브 주소를 처리하는데 오류가 생겼다네:\n{}".format(e.args[0]))

                    else:
                        # Probably an error from a different extractor, but I've only seen youtube's
                        log.error("\"{url}\": {ex}를 처리하는데 오류가 생긴것 같다네".format(url=song_url, ex=e))

                    await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=True)
                    continue

                except Exception as e:
                    log.error("\"{url}\": {ex}를 처리하는데 오류가 생긴것 같다네".format(url=song_url, ex=e))
                    log.exception()

                    self.autoplaylist.remove(song_url)
                    continue

                if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                    log.debug("재생목록을 찾은것 같은데, 지금은 지원하지 않고있다네. 건너뛰겠네.")
                    # TODO: Playlist expansion

                # Do I check the initial conditions again?
                # not (not player.playlist.entries and not player.current_entry and self.config.auto_playlist)

                try:
                    await player.playlist.add_entry(song_url, channel=None, author=None)
                except exceptions.ExtractionError as e:
                    log.error("자동재생 목록 {}에서 곡을 가져오는데 오류가 생긴 것 같다네".format(e))
                    log.debug('', exc_info=True)
                    continue

                break

            if not self.autoplaylist:
                # TODO: When I add playlist expansion, make sure that's not happening during this check
                log.warning("자동재생 목록에서 실행 가능한 곡이 없는것같다네. 사용을 해제하겠네.")
                self.config.auto_playlist = False

        else: # Don't serialize for autoplaylist events
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_entry_added(self, player, playlist, entry, **_):
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nFFmpeg에서 에러가 발생했다네:\n{}\n```".format(ex)
            )
        else:
            log.exception("사용자 오류가 발생했다네", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None

        if not self.config.status_message:
            if self.user.bot:
                activeplayers = sum(1 for p in self.players.values() if p.is_playing)
                if activeplayers > 1:
                    game = discord.Game(type=0, name="%s 개 서버에서 음악이 실행되고 있다네!" % activeplayers)
                    entry = None

                elif activeplayers == 1:
                    player = discord.utils.get(self.players.values(), is_playing=True)
                    entry = player.current_entry

            if entry:
                prefix = u'\u275A\u275A ' if is_paused else ''

                name = u'{}{}'.format(prefix, entry.title)[:128]
                game = discord.Game(type=0, name=name)
        else:
            game = discord.Game(type=0, name=self.config.status_message.strip()[:128])

        async with self.aiolocks[_func_()]:
            if game != self.last_status:
                await self.change_presence(game=game)
                self.last_status = game

    async def update_now_playing_message(self, server, message, *, channel=None):
        lnp = self.server_specific_data[server]['last_np_msg']
        m = None

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp: # If there was a previous lp message
            oldchannel = lnp.channel

            if lnp.channel == oldchannel: # If we have a channel to update it in
                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != lnp and lnp: # If we need to resend it
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.safe_send_message(channel, message, quiet=True)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel: # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(channel, message, quiet=True)

            else: # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.safe_send_message(oldchannel, message, quiet=True)

        elif channel: # No previous message
            m = await self.safe_send_message(channel, message, quiet=True)

        self.server_specific_data[server]['last_np_msg'] = m


    async def serialize_queue(self, server, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(server)
        if not player:
            return

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization'+':'+server.id]:
            log.debug("%s의 요청 목록을 직렬화 하는중이라네", server.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.servers]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, server, voice_client, playlist=None, *, dir=None) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer.  If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'data/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            if not os.path.isfile(dir):
                return None

            log.debug("%s의 요청 목록을 비직렬화 하는중이라네", server.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    @ensure_appinfo
    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # playlists in autoplaylist
        await self._scheck_autoplaylist()

        # config/permissions async validate?
        await self._scheck_configs()


    async def _scheck_ensure_env(self):
        log.debug("데이터 폴더가 존재하는지 확인중이라네")
        for server in self.servers:
            pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)

        with open('data/server_names.txt', 'w', encoding='utf8') as f:
            for server in sorted(self.servers, key=lambda s:int(s.id)):
                f.write('{:<22} {}\n'.format(server.id, server.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("오래된 음악 저장소를 정리했다네")
            else:
                log.debug("오래된 음악 저장소를 정리할수가 없었다네. 일단 계속하겠네.")


    async def _scheck_server_permissions(self):
        log.debug("서버 권한을 확인하는 중이라네")
        pass # TODO

    async def _scheck_autoplaylist(self):
        log.debug("자동재생 목록을 확인하고 있다네")
        pass # TODO

    async def _scheck_configs(self):
        log.debug("설정을 확인하는 중이라네")
        await self.config.async_validate(self)



#######################################################################################################################


    async def safe_send_message(self, dest, content, **kwargs):
        tts = kwargs.pop('tts', False)
        quiet = kwargs.pop('quiet', False)
        expire_in = kwargs.pop('expire_in', 0)
        allow_none = kwargs.pop('allow_none', True)
        also_delete = kwargs.pop('also_delete', None)

        msg = None
        lfunc = log.debug if quiet else log.warning

        try:
            if content is not None or allow_none:
                msg = await self.send_message(dest, content, tts=tts)

        except discord.Forbidden:
            lfunc("\"%s\"로 문자를 보낼수가 없는것 같다네. 권한이 없는것 같다네.", dest.name)

        except discord.NotFound:
            lfunc("\"%s\"로 문자를 보낼수가 없는것 같다네. 올바른 채팅방이 맞는가?", dest.name)

        except discord.HTTPException:
            if len(content) > DISCORD_MSG_CHAR_LIMIT:
                lfunc("문자가 최대 글자수를 넘은것 같다네! (%s)자", DISCORD_MSG_CHAR_LIMIT)
            else:
                lfunc("문자를 보내는데 실패한것 같다네")
                log.noise(" %s로  %s를 보내는데 HTTPException이 발생한 것 같다네", dest, content)

        finally:
            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            lfunc("\"{}\" 문자를 삭제할 수 없는것 같다네. 권한이 없는것 같다네 ".format(message.clean_content))

        except discord.NotFound:
            lfunc("\"{}\" 문자를 삭제할 수 없는것 같다네. 문자를 못찾겠다네".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            lfunc("\"{}\" 문자를 삭제할 수 없는것 같다네. 문자를 못찾겠다네.".format(message.clean_content))
            if send_if_fail:
                lfunc("문자를 대신 전송하고 있다네")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            log.warning("{}에 문자를 보낼수가 없는것 같다네. 권한이 없는것 같다네".format(destination))

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)


    async def restart(self):
        self.exit_signal = exceptions.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "음악봇이 로그인을 할수 없다네! 인증키가 잘못된것 같다네.",
                "옵션 파일에 있는 %s를 수정해주겠나.  "
                "각 항목이 제 위치에 적혀있는지 확인해주겠나!"
                % ['shit', '토큰', '이메일/비밀번호', '인증키'][len(self.config.auth)]
            ) #     ^^^^ In theory self.config.auth should never have no items

        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("정리를 하는데 에러가 발생했다네", exc_info=True)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            log.error("{}에 에러가 발생했다네\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("{}에 에러가 발생했다네".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\n디스코드에 재접속했다네!\n")

    async def on_ready(self):
        dlogger = logging.getLogger('discord')
        for h in dlogger.handlers:
            if getattr(h, 'terminator', None) == '':
                dlogger.removeHandler(h)
                print()

        log.debug("연결에 성공했다네! 이제 시작해도 될걸세!")

        self.ws._keep_alive.name = '연결을 살려'

        if self.init_ok:
            log.debug("'준비' 상태로 됬다는 문자를 다시 받은것 같다네! 재시작에 실패한것 같기도 하다네.")
            return

        await self._on_ready_sanity_checks()
        print()

        log.info('디스코드에 연결을 성공했다네!')

        self.init_ok = True

        ################################

        log.info("봇:   {0}/{1}#{2}{3}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator,
            ' [봇]' if self.user.bot else ' [사용자봇]'
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            log.info("주인: {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('서버 목록이라네:')
            [log.info(' - ' + s.name) for s in self.servers]

        elif self.servers:
            log.warning("어떠한 서버에서도 주인님을 찾을수 없었다네. (사용자: %s)\n" % self.config.owner_id)

            log.info('서버 목록이라네:')
            [log.info(' - ' + s.name) for s in self.servers]

        else:
            log.warning("주인님이 누군지 모르고, 음악봇은 어떠한 서버에 가지도 않았다네.")
            if self.user.bot:
                log.warning(
                    "음악봇이 서버에 참가하도록 하려면, 이 링크를 인터넷 창에 붙여넣어주겠나. \n"
                    "주의: 주 계정으로 로그인 되어있는 상태여야하며, 서버 관리 권한이\n"
                    "있어야 원하는 봇을 참가시킬 수 있다네.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)

            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            if chlist:
                log.info("문자 채팅방에 접근 시도:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                print("문자 채팅방에 들어가지 않았다네")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("음성채팅방에 들어가지 않았다네:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print(flush=True)

        else:
            log.info("문자 채팅방에 들어가지 않았다네")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)

            invalids = set()
            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)

            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            if chlist:
                log.info("음성채팅방에 자동으로 들어가는 중이라네:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
            else:
                log.info("음성채팅방에 자동으로 들어가지 않고 있다네")

            if invalids and self.config.debug_mode:
                print(flush=True)
                log.info("문자 채널에 자동으로 들어갈 수 없는것 같다네:")
                [log.info(' - {}/{}'.format(ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            log.info("어떠한 음성채팅방에도 들어가지 않고 있다네")
            autojoin_channels = set()

        print(flush=True)
        log.info("설정:")

        log.info("  명령어 앞에 붙일것: " + self.config.command_prefix)
        log.info("  기본 음량: {}%".format(int(self.config.default_volume * 100)))
        log.info("  건너뛰기: {} 표 투표 {}%".format(
            self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
        log.info("  현재 재생중 @멘션: " + ['거짓', '참'][self.config.now_playing_mentions])
        log.info("  자동-방참가: " + ['거짓', '참'][self.config.auto_summon])
        log.info("  자동-재생목록: " + ['거짓', '참'][self.config.auto_playlist])
        log.info("  자동-멈춤: " + ['거짓', '참'][self.config.auto_pause])
        log.info("  문자 삭제: " + ['거짓', '참'][self.config.delete_messages])
        if self.config.delete_messages:
            log.info("    삭제 호출: " + ['거짓', '참'][self.config.delete_invoking])
        log.info("  디버그 모드: " + ['거짓', '참'][self.config.debug_mode])
        log.info("  음악들은 " + ['저장이 안된다네', '저장된다네'][self.config.save_videos])
        if self.config.status_message:
            log.info("  상태 메시지: " + self.config.status_message)
        print(flush=True)

        await self.update_now_playing_status()

        # maybe option to leave the ownerid blank and generate a random command for the owner to use
        # wait_for_message is pretty neato

        await self._join_startup_channels(autojoin_channels, autosummon=self.config.auto_summon)

        # t-t-th-th-that's all folks!

    async def cmd_help(self, command=None):
        """
        사용법:
            {command_prefix}도움말 [명령어]

        별칭:
            {command_prefix}도움, {command_prefix}man, {command_prefix}help
        
        도움말을 출력해준다네.

        명령어를 같이 적으셨다면, 해당 명령어에 대한 정보를 알려준다네.
        아니라면 사용 가능한 명령어들을 출력해준다네.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd and not hasattr(cmd, 'dev_cmd'):
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__)
                    ).format(command_prefix=self.config.command_prefix),
                    delete_after=60
                )
            else:
                return Response("그런 명령은 없는것같다네.", delete_after=10)

        else:
            helpmsg = "**사용 가능한 명령어들**\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help' and not hasattr(getattr(self, att), 'dev_cmd'):
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(self.config.command_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "```\n[영문 명령어]<https://just-some-bots.github.io/MusicBot/#guidescommands>"
            helpmsg += "\n\n[한글 명령어] 그런거 없다네! 봇주한테 물어보라네! "
            helpmsg += "```\n중복되는 명령어들이 장난아니게 많다네!\n"
            helpmsg += "\n영문 명령들은 원래 사용방법 그대로 냅뒀다네\n"
            helpmsg += "\n모르겠다면 {}처음 이나 {}도움2 명령을 사용해보겠나!\n"
            helpmsg += "```\n{}도움 명령어 를 사용해서 더 자세하게 알아볼수도 있다네!".format(self.config.command_prefix, self.config.command_prefix, self.config.command_prefix)
            

            return Response(helpmsg, reply=True, delete_after=20)
    cmd_도움 = cmd_도움말 = cmd_man = cmd_help

    async def cmd_id(self, author, user_mentions):
        """
        사용법:
            {command_prefix}아이디 [@사용자]
        별칭:
            {command_prefix}id

        다른 사용자나 부르신 분의 아이디를 알려준다네.
        """
        if not user_mentions:
            return Response('자네의 아이디는 `%s` 인것 같다네!' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s의 아이디는 `%s`인것 같다네!" % (usr.name, usr.id), reply=True, delete_after=35)
    cmd_아이디 = cmd_id

    @owner_only
    async def cmd_joinserver(self, message, server_link=None):
        """
        사용법:
            {command_prefix}서버추가  서버주소
        별칭:
            {command_prefix}joinserver
        
        봇에게 들어올 서버를 쥐어주는 명령이라네.
        주의: 봇은 참여링크는 사용할수 없다네.
        """

        if self.user.bot:
            url = await self.generate_invite_link()
            return Response(
                "이곳을 눌러서 서버에 추가해주겠나!: \n{}".format(url),
                reply=True, delete_after=20
            )

        try:
            if server_link:
                await self.accept_invite(server_link)
                return Response("\N{THUMBS UP SIGN}")

        except:
            raise exceptions.CommandError('사용할수 없는 주소를 주셨다네.:\n{}\n'.format(server_link), expire_in=30)
    cmd_서버추가 = cmd_joinserver

    #deprecated. maybe?
    async def cmd_oldplay(self, player, channel, author, leftover_args, song_url):
        """
        사용법:
            {command_prefix}play  음악주소
            {command_prefix}play  찾을곡
        
        재생목록에 곡을 추가해준다네.
        음악 주소가 주어지지 않았을경우('찾을곡' 인 경우), 유튜브 검색결과중 첫번째 결과를 재생목록에 추가해준다네
        """

        song_url = song_url.strip('<>')

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
        song_url = urllib.parse.unquote(song_url)
        linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        if matchUrl is None:
            song_url = song_url.replace('/', '%2F')
        async with self.aiolocks[_func_() + ':' + author.id]:
            try:
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            except Exception as e:
                raise exceptions.CommandError(e, expire_in=30)

            if not info:
                raise exceptions.CommandError(
                    "이 동영상은 실행할수 없는거같다네. {}생방송 명령어를 사용해보겠나!".format(self.config.command_prefix),
                    expire_in=30
                )

            # abstract the search handling away from the user
            # our ytdl options allow us to use search strings as input urls
            if info.get('url', '').startswith('ytsearch'):
                # print("[Command:play] Searching for \"%s\"" % song_url)
                info = await self.downloader.extract_info(
                    player.playlist.loop,
                    song_url,
                    download=False,
                    process=True,    # ASYNC LAMBDAS WHEN
                    on_error=lambda e: asyncio.ensure_future(
                        self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                    retry_on_error=True
                )

                if not info:
                    raise exceptions.CommandError(
                        "검색한 것에서 정보를 알아보는데 실패했다네. youtubed1이 아무런 정보도 주지 않은것같네. "
                        "이 문제가 계속되면 재시작해주겠나! ", expire_in=30
                    )

                if not all(info.get('entries', [])):
                    # empty list, no data
                    log.debug("비어있는 목록만 받았다네. 정보가 없는것같다네.")
                    return

                # TODO: handle 'webpage_url' being 'ytsearch:.' or extractor type
                song_url = info['entries'][0]['webpage_url']
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                # Now I could just do: return await self.cmd_oldplay(player, channel, author, song_url)
                # But this is probably fine

            # TODO: Possibly add another check here to see about things like the bandcamp issue
            # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls
            if 'entries' in info:
                
                # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
                num_songs = sum(1 for _ in info['entries'])
                if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                    try:
                        return await self._cmd_playlist_async(player, channel, author, song_url, info['extractor'])
                    except exceptions.CommandError:
                        raise
                    except Exception as e:
                        log.error("재생목록에 추가하는데 실패했다네.", exc_info=True)
                        raise exceptions.CommandError("재생목록에 추가 실패:\n%s" % e, expire_in=30)

                t0 = time.time()

                # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                # I don't think we can hook into it anyways, so this will have to do.
                # It would probably be a thread to check a few playlists and get the speed from that
                # Different playlists might download at different speeds though
                wait_per_song = 1.2

                procmesg = await self.safe_send_message(
                    channel,
                    '{} 개 곡에 대하여 정보를 모으는중이라네!{}'.format(
                        num_songs,
                        ', 예상시간: {} 초'.format(fixg(
                            num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                # We don't have a pretty way of doing this yet.  We need either a loop
                # that sends these every 10 seconds or a nice context manager.
                await self.send_typing(channel)

                # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
                #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

                entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

                tnow = time.time()
                ttime = tnow - t0
                listlen = len(entry_list)
                drop_count = 0

                log.info("{} 개 곡을 {} 초에, {:.2f}초/곡 으로 처리 완료했다네!, {:+.2g}/곡 의 예상시간은 ({}초)".format(
                    listlen,
                    fixg(ttime),
                    ttime / listlen if listlen else 0,
                    ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                    fixg(wait_per_song * num_songs))
                )

                await self.safe_delete_message(procmesg)

                if not listlen - drop_count:
                    raise exceptions.CommandError(
                        "추가된 곡이 없다네! \n모든 곡이 최대 대기시간을 넘어갔다네. ",
                        expire_in=30
                    )

                reply_text = "**%s** 개 곡을 재생목록에 추가했다네. \n재생목록에서의 현재 위치는 %s번 이라네!"
                btext = str(listlen - drop_count)

            else:
                try:
                    entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

                except exceptions.WrongEntryTypeError as e:
                    if e.use_url == song_url:
                        log.warning("목록에는 이상이 있는데. 주신 주소는 정상인거같다네.  \n도와주겠나..")

                    log.debug("주신 주소인 \"%s\" => 단일 곡이 아니라, 노래목록이 아닐까 생각되네" % song_url)
                    log.debug("\"%s\" 를 대신 사용해보겠나!" % e.use_url)

                    return await self.cmd_oldplay(player, channel, author, leftover_args, e.use_url)

                reply_text = "재생목록에 **%s** 를 추가했다네. \n재생목록에서의 현재 위치는 %s번 이라네!"
                btext = entry.title

            if position == 1 and player.is_stopped:
                position = '다음곡으로 가겠네!'
                reply_text %= (btext, position)

            else:
                try:
                    time_until = await player.playlist.estimate_time_until(position, player)
                    reply_text += '\n- 재생까지 남은 시간 예상은: %s 이라네!'
                except:
                    traceback.print_exc()
                    time_until = ''

                reply_text %= (btext, position, ftimedelta(time_until))

        return Response(reply_text, delete_after=15)

    async def _cmd_playlist_async(self, player, channel, author, playlist_url, extractor_type):
        """
        async wizardry를 사용해서 재생목록에 멈추는것 없이 추가할수 있게 하는 숨겨진 handler라네
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("그 재생목록은 재생할수 없을것같다네.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "%s 개 곡 처리중." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("재생목록을 처리하는데 문제가 생겼다네.", exc_info=True)
                raise exceptions.CommandError('재생목록에 %s 추가하는데 문제가 생긴것같다네.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("재생목록을 처리하는데 문제가 생겼다네.", exc_info=True)
                raise exceptions.CommandError('재생목록에 %s 추가하는데 문제가 생긴것같다네.' % playlist_url, expire_in=30)


        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        
        log.info("{} 개 곡을 {} 초에, {:.2f}초/곡 으로 처리 완료했다네!\n {:+.2g}/곡 의 예상시간은 ({}초)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "추가된 곡이 없다네! \n모든 곡이 최대 대기시간을 넘어갔다네."
            if skipped:
                basetext += "\n그리고, 현재 곡은 너무 길어서 건너뛰었다네."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("추가된 {} 곡들은 {} 초 후에 시작될 예정이라네!".format(
            songs_added, fixg(ttime, 1)), delete_after=15)
    

    async def cmd_np(self, player, channel, server, message):
        """
        사용법:
            {command_prefix}재생중

        별칭:
            {command_prefix}지금곡, {command_prefix}nowplaying, {command_prefix}np
        
        현재 재생중인 곡을 알려준다네.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))

            streaming = isinstance(player.current_entry, StreamPlaylistEntry)
            prog_str = ('`[{progress}]`' if streaming else '`[{progress}/{total}]`').format(
                progress=song_progress, total=song_total
            )
            action_text = '생방송중' if streaming else '재생중'

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "{action}: **{title}** - **{author}** 에 의해 추가됬다네 {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>".format(
                    action=action_text,
                    title=player.current_entry.title,
                    author=player.current_entry.meta['author'].name,
                    progress=prog_str,
                    url=player.current_entry.url
                )
            else:
                np_text = "{action}: **{title}** {progress}\n\N{WHITE RIGHT POINTING BACKHAND INDEX} <{url}>".format(
                    action=action_text,
                    title=player.current_entry.title,
                    progress=prog_str,
                    url=player.current_entry.url
                )

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                '재생목록에 추가된 곡이 없다네! \n{}재생을 사용해서 곡을 추가해주겠나.'.format(self.config.command_prefix),
                delete_after=15
            )
    cmd_지금곡 = cmd_재생중 = cmd_nowplaying = cmd_np

    async def cmd_summon(self, channel, server, author, voice_channel):
        """
        사용법:
            {command_prefix}들어와

        별칭:
            {command_prefix}드루와, {command_prefix}드러와, {command_prefix}이리와,{command_prefix}소환, 
            {command_prefix}입대, {command_prefix}입장, {command_prefix}summon
        

        호출한 자의 음악채널로 이 봇을 입장시키는 명령이라네.
        """

        if not author.voice_channel:
            raise exceptions.CommandError('음성채팅방에 들어가 있지 아니하지 않은가!')

        voice_client = self.voice_client_in(server)
        if voice_client and server == author.voice_channel.server:
            await voice_client.move_to(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(server.me)

        if not chperms.connect:
            log.warning(" \"{}\" 에 들어갈수가 없다네. 권한이 없는것 같다네.".format(author.voice_channel.name))
            return Response(
                "```\"{}\"에 들어갈수가 없다네. 권한이 없는것 같다네.```".format(author.voice_channel.name),
                delete_after=25
            )

        elif not chperms.speak:
            log.warning("\"{}\"에 들어가지 않을것이라네! 들어가서 말할수 있는 권한이 없다네!!".format(author.voice_channel.name))
            return Response(
                "```\"{}\"에 들어가지 않을것이라네! 들어가서 말할수 있는 권한이 없다네!!```".format(author.voice_channel.name),
                delete_after=25
            )

        log.info("{0.server.name}/{0.name} 참가중".format(author.voice_channel))

        player = await self.get_player(author.voice_channel, create=True, deserialize=self.config.persistent_queue)

        if player.is_stopped:
            player.play()

        if self.config.auto_playlist:
            await self.on_player_finished_playing(player)
    cmd_입장 = cmd_입대 = cmd_소환 = cmd_이리와 = cmd_드루와 = cmd_드러와 = cmd_들어와 = cmd_summon

    async def cmd_pause(self, player):
        """
        사용법:
            {command_prefix}일시정지

        별칭:
            {command_prefix}스톱, {command_prefix}잠깐, {command_prefix}잠깐만, {command_prefix}resume
        
        현재 재생중인 곡을 일시정지 시켜준다네.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError('재생중인 곡이 없다네.', expire_in=30)
    cmd_일시정지 = cmd_스톱 = cmd_잠깐 = cmd_잠깐만 = cmd_pause

    async def cmd_resume(self, player):
        """
        사용법:
            {command_prefix}재개

        별칭:
            {command_prefix}정지해제, {command_prefix}정지풀기, {command_prefix}다시시작, {command_prefix}resume

        일시정지된 곡을 다시 실행시켜준다네.
        """

        if player.is_paused:
            player.resume()

        else:
            raise exceptions.CommandError('일시정지된 곡이 없다네.', expire_in=30)
    cmd_정지해제 = cmd_정지풀기 = cmd_재개 = cmd_다시시작 = cmd_resume

    async def cmd_shuffle(self, channel, player):
        """
        사용법:
            {command_prefix}섞기

        별칭:
            {command_prefix}무작위,{command_prefix}shuffle

        재생목록을 무작위로 섞어준다네.
        """

        player.playlist.shuffle()

        cards = ['\N{BLACK SPADE SUIT}', '\N{BLACK CLUB SUIT}', '\N{BLACK HEART SUIT}', '\N{BLACK DIAMOND SUIT}']
        random.shuffle(cards)

        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response("\N{OK HAND SIGN}", delete_after=15)
    cmd_섞기 = cmd_무작위 = cmd_shuffle

    async def cmd_clear(self, player, author):
        """
        사용법:
            {command_prefix}재생목록지우기

        별칭:
            {command_prefix}비우기, {command_prefix}재생목록비우기, {command_prefix}목록지우기, {command_prefix}큐지우기, 
            {command_prefix}목록비우기, {command_prefix}큐비우기,{command_prefix}skip
        
        재생목록을 비워준다네.
        """

        player.playlist.clear()
        return Response('\N{PUT LITTER IN ITS PLACE SYMBOL}', delete_after=10)
    cmd_비우기 = cmd_목록비우기 = cmd_재생목록비우기 = cmd_큐비우기 = cmd_목록비우기 = cmd_재생목록비우기 = cmd_큐지우기 = cmd_clear

    async def cmd_skip(self, player, channel, author, message, voice_channel):
        """
        사용법:
            {command_prefix}건너뛰기

        별칭:
            {command_prefix}다음, {command_prefix}넘겨, {command_prefix}스킵, {command_prefix}skip

        다음곡으로 넘어간다네.
        """

        if player.is_stopped:
            raise exceptions.CommandError("곡을 건너뛸수 없다네. \n재생중인 곡이 없는것 같네!", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response("다음 곡인 (%s) 저장중이라네! 기다려주겠나." % player.playlist.peek().title)
                elif player.playlist.peek().is_downloaded:
                    print("다음 곡이 곧 시작될거라네! \n잠시만 기다려주겠나.")
                else:
                    print("뭔가 이상하게 작동하고 있는거같다네.  "
                          "이 문제가 계속되면 저를 재시작해주겠나!")
            else:
                print("뭔가 이상하게 작동하고 있는거같다네.  "
                      "이 문제가 계속되면 저를 재시작해주겠나!")
        

        await self.cmd_clean(message, channel, channel.server, author)
        await self.cmd_queue(channel, player)
        if author.id == self.config.owner_id \
                or author == player.current_entry.meta.get('author', None):

            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        player.skip()  # check autopause stuff here        
        '''
        return Response(
            '**{}** 곡을 건너뛰겠네.'
            '\n건너뛰는 것에 대한 투표가 가결되었다네.{}'.format(
                player.current_entry.title,
                ' 바로 다음곡을 시작하겠네!' if player.playlist.peek() else ''
            ),
            reply=True,
            delete_after=10
        )
        '''

    cmd_건너뛰기 = cmd_넘겨 = cmd_스킵 = cmd_다음 = cmd_skip

    async def cmd_volume(self, message, player, new_volume=None):
        """
        사용법:
            {command_prefix}음량  (+/-)[음량]
        별칭:
            {command_prefix}소리, {command_prefix}볼륨, {command_prefix}volume
        
        
        재생시의 음량을 조절해준다네. 1부터 100까지 값만 받고있다네.
        [음량] 앞에 + 나 -를 추가해서 현재 볼륨 기준으로 조절할수도 있다네.
        """

        if not new_volume:
            return Response('현재 음량: `%s%%`' % int(player.volume * 100), reply=True, delete_after=10)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError('{} 는 제대로된 숫자가 아니라네'.format(new_volume), expire_in=20)

        vol_change = None
        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('음량이 %d 에서 %d 로 변경되었다네' % (old_volume, new_volume), reply=True, delete_after=10)

        else:
            if relative:
                raise exceptions.CommandError(
                    '음량이 이상하게 변경되었다네.: {}{:+} -> {}%. {} 에서 {:+}. 사이의 값으로 해주겠나'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    '음량이 이상하게 변경되었다네.: {}%. 1에서 100 사이의 값으로 해주겠나.'.format(new_volume), expire_in=20)
    cmd_소리 = cmd_음량 = cmd_볼륨 = cmd_volume

    async def cmd_queue(self, channel, player):
        """
        사용법:
            {command_prefix}재생목록

        별칭:
            {command_prefix}큐, {command_prefix}목록, {command_prefix}리스트, 
            {command_prefix}list, {command_prefix}queue

        현재 재생목록을 보여준다네.
        """
        lines = []
        unlisted = 0
        andmoretext = '* . 그리고 %s 개 더있다네 *' % ('x' * len(player.playlist.entries))
        #emptymessage = await self.safe_send_message(channel, '\n')
        #if(emptymessage is not None):
        #    await self.cmd_clean(emptymessage, channel, channel.server, emptymessage.author)
        last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
        if(last_np_msg is not None):
            await self.cmd_clean(last_np_msg, channel, channel.server, last_np_msg.author)
        total_time = 0
        if player.current_entry:
            # TODO: Fix timedelta garbage with util function
            #song_progress = ftimedelta(timedelta(seconds=player.progress))
            #song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            #prog_str = '`[%s/%s]`' % (song_progress, song_total)
            total_time = total_time + player.current_entry.duration
            song_total = '`[%s]`' % (ftimedelta(timedelta(seconds=player.current_entry.duration)))

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append("현재 재생중인건 :\n **%s** - **%s** 가 추가한 곡이라네! %s \n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, song_total))
            else:
                lines.append("현재 재생중인건 :\n **%s** %s\n" % (player.current_entry.title, song_total))
                
        for i, item in enumerate(player.playlist, 1):
            total_time = total_time + item.duration
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = '`{}.` **{}**  |> **{}** 가 추가한 곡이라네! {}'.format(i, item.title, item.meta['author'].name, ftimedelta(timedelta(seconds=item.duration))).strip()
            else:
                nextline = '`{}.` **{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*. 그리고 %s 개 더있다네*' % unlisted)

        if not lines:
            lines.append(
                '재생목록에 추가된 곡이 없다네! {}재생을 사용해서 곡을 추가해주겠나.'.format(self.config.command_prefix))
        song_total = '`[%s]`' % (ftimedelta(timedelta(seconds=total_time)))
        lines.append('\n 총 재생시간은 **%s** 이라네' % (song_total))
        message = '\n'.join(lines)
        await self.safe_send_message(channel, message)
        #return Response(message, delete_after=15)
    cmd_큐 = cmd_목록 = cmd_재생목록 = cmd_리스트 = cmd_list = cmd_queue

    async def cmd_clean(self, message, channel, server, author, search_range=10):
        """
        사용법:
            {command_prefix}청소 [갯수]

        별칭:
            {command_prefix}크린, {command_prefix}슥삭 , {command_prefix}청소, {command_prefix}청소기,
            {command_prefix}스윽, {command_prefix}스으윽 , {command_prefix}지우자, {command_prefix}지우기,
            {command_prefix}치우자, {command_prefix}치우기, {command_prefix}clean

        예시:
            {command_prefix}청소
            {command_prefix}청소  20  
        
        이 봇이 말한 메세지를 [갯수] 만큼 지워준다네.
        기본 [갯수] 값으로는 10이고 최대 1000이긴 한다네
        """
        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("숫자를 입력해주겠나.  숫자를, 10진수로!  `15`같은걸로 말일세.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('{} 개 대화를 삭제했다네!{}'.format(len(deleted), '\n노래관련 명령어만 입력해주겠나!' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('{} 개 대화를 삭제했다네{}.'.format(deleted, ' ' * bool(deleted)), delete_after=6)
    cmd_치우기 = cmd_치우자 = cmd_지우기 = cmd_지우자 = cmd_스윽 = cmd_스으윽 = cmd_청소기 = cmd_청소 = cmd_슥삭 = cmd_크린 = cmd_clean

    async def cmd_perms(self, author, channel, server, permissions):
        """
        사용법:
            {command_prefix}perms

        별칭:
            {command_prefix}권한
        
        가지고 있는 권한 목록을 보여준다네.
        """

        lines = ['%s 서버에서 가지고 있는 권한은.\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=10)
    cmd_권한 = cmd_perms

    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        사용법:
            {command_prefix}이름  변경될이름

        별칭:
            {command_prefix}이름변경, {command_prefix}setname

        봇의 이름을 변경한다네.
        주의: 이 명령은 1시간에 2번으로 제한되어있다네! (디스코드 서버 규칙)
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)

        except discord.HTTPException:
            raise exceptions.CommandError(
                "이름 변경에 실패한거같다네. 혹시 너무 많이 시도한건가?"
                "1시간에 2번밖에 변경을 할수 없다네!")

        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=10)
    cmd_이름설정 = cmd_setname

    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        사용법:
            {command_prefix}별명  변경될별명

        별칭:
            {command_prefix}별명설정, {command_prefix}setnick

        봇의 별명을 변경한다네.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("별명을 변경할수 없다네. 권한이 없는것같다네!")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=10)
    cmd_별명설정 = cmd_setnick

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        사용법:
            {command_prefix}setavatar [주소]
        
        봇의 아바타를 변경해준다네!
        파일을 첨부하거나 주소를 공란으로 해도 작동은 된다네.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        elif url:
            thing = url.strip('<>')
        else:
            raise exceptions.CommandError("주소나 첨부파일을 주어야 한다네.", expire_in=20)

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("아바타를 변경할수 없는거같다네: {}".format(e), expire_in=20)

        return Response("\N{OK HAND SIGN}",delete_after=10)
    cmd_아바타설정 = cmd_setnick

    async def cmd_disconnect(self, server):
        """
        사용법:
            {command_prefix}돌아가
        별칭:
            {command_prefix}잘가, {command_prefix}연결해제, {command_prefix}저리가, {command_prefix}꺼져, {command_prefix}탈영, {command_prefix}탈주, 
            {command_prefix}닌자, {command_prefix}전역, {command_prefix}나가, {command_prefix}퇴장, {command_prefix}disconnect
        
        해당 음성채팅방에서 나가게 된다네!
        """
        await self.disconnect_voice_client(server)
        return Response("\N{DASH SYMBOL}", delete_after=10)
    cmd_퇴장 = cmd_꺼져 = cmd_나가 = cmd_전역 = cmd_닌자 = cmd_탈주 = cmd_탈영 = cmd_돌아가 = cmd_저리가 = cmd_잘가 = cmd_연결해제 = cmd_disconnect

    async def cmd_restart(self, channel):
        """
        사용법:
            {command_prefix}서버재시작
        별칭:
            {command_prefix}재시작, {command_prefix}restart
        
        서버를 재시작하게 된다네!
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal()
    cmd_재시작 = cmd_서버재시작 = cmd_restart

    async def cmd_shutdown(self, channel):
        """
        사용법:
            {command_prefix}서버종료
        별칭:
            {command_prefix}종료, {command_prefix}shutdown
        
        서버를 종료하게 된다네!
        * 일단은 종료인데 서버측에서 다시 살릴수도 있다네!
        """
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal()
    cmd_종료 = cmd_서버종료 = cmd_shutdown

    async def check_message(self,message):
        message_content = message.content.strip()
        linksRegex = "((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)"
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(message_content)
        if matchUrl is not None:
            player = await self.get_player(message.author.voice_channel, create=True, deserialize=self.config.persistent_queue)
            await self.cmd_oldplay(player, message.channel, message.author, [], message_content )
            await self.cmd_옥시크린(message, message.channel, message.server, message.author, 1)
            return True
        return False

    async def on_message(self, message):
        await self.wait_until_ready()

        message_content = message.content.strip()
        #if await self.check_message(message):
        #    return
        if not message_content.startswith(self.config.command_prefix):
            return

        if message.author == self.user:
            log.warning("자기 자신에 대한 명령은 흘려 들을것이라네! ({})".format(message.content))
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return  # if I want to log this I just move it under the prefix check

        command, *args = message_content.split(' ')  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        #print(self.config.command_prefix + command)
        command = command[len(self.config.command_prefix):].lower().strip()
        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            return
            '''
            #주의
            #cmd_재생에서 Waiting 처리를 하려다가 남은 코드라네
            #일단은 모든 메세지를 print해서(바로 위의 코드, if부터 2줄위) 처리는 됬는데
            #남의 메세지도 intercept 될거같긴한다네.
            #일단은 불편을 감수하고 써야할거같다네 (귀찮음) 
            try:
                int(command)
            except ValueError:
                return
            # 조심! 5 as Max Number [0~5]
            if int(command) > 0 and int(command) < 6 :
                print(self.config.command_prefix + command)
                await self.safe_delete_message(message)
                return
            else:
                return
            '''
        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.send_message(message.channel, '이 봇은 개인 대화에 사용할수 없다네.')
                return

        if message.author.id in self.blacklist and message.author.id != self.config.owner_id:
            log.warning("차단된 사용자들은: {0.id}/{0!s} ({1})".format(message.author, command))
            return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n. ')))
        
        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()
        sentmsg = response = None
        # noinspection PyBroadException
        
        try:

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.server)

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args
            args_expected = []
            for key, param in list(params.items()):
                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )
                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=20
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '{}, {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            log.error("[오류] {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n{}\n```'.format(e.message),
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("on_message에 예외발생", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            if not sentmsg and not response and self.config.delete_invoking:
                await asyncio.sleep(5)
                await self.safe_delete_message(message, quiet=True)


    async def on_voice_state_update(self, before, after):
        if not self.init_ok:
            return # Ignore stuff before ready

        state = VoiceStateUpdate(before, after)

        if state.broken:
            log.voicedebug("음성 상태 업데이트가 실패했다네.")
            return

        if state.resuming:
            log.debug("{0.server.name}/{0.name}에의 음성 연결이 재개되었다네".format(state.voice_channel))

        if not state.changes:
            log.voicedebug("음성 상태 업데이트가 비어있는것 같군? 세션id가 중간에 변경된것 같다네")
            return # Session id change, pointless event

        ################################

        log.voicedebug("{mem.id}/{mem!s} 의 음성 연결 상태 업데이트는 {ser.name}/{vch.name} -> {dif}".format(
            mem = state.member,
            ser = state.server,
            vch = state.voice_channel,
            dif = state.changes
        ))

        if not state.is_about_my_voice_channel:
            return # Irrelevant channel

        if state.joining or state.leaving:
            log.info("{0.id}/{0!s} 는 {1} {2}/{3}에".format(
                state.member,
                '참가했다네' if state.joining else '떠났다네',
                state.server,
                state.my_voice_channel
            ))

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.server.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(state.my_voice_channel)

        if state.joining and state.empty() and player.is_playing:
            log.info(autopause_msg.format(
                state = "일시중지",
                channel = state.my_voice_channel,
                reason = "(빈 채팅방에 들어가려했다네)"
            ).strip())

            self.server_specific_data[after.server]['auto_paused'] = True
            player.pause()
            return

        if not state.is_about_me:
            if not state.empty(old_channel=state.leaving):
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state = "재개",
                        channel = state.my_voice_channel,
                        reason = ""
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = False
                    player.resume()
            else:
                if not auto_paused and player.is_playing:
                    log.info(autopause_msg.format(
                        state = "일시중지",
                        channel = state.my_voice_channel,
                        reason = "(empty channel)"
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = True
                    player.pause()


    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            log.warning("서버 \"%s\" 가 지역을 바꾸었다네: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)


    async def on_server_join(self, server:discord.Server):
        log.info("봇이 방금 서버에 참가했다네!: {}".format(server.name))

        if not self.user.bot:
            alertmsg = "<@{uid}> 안녕하신가! 나는 음악봇이고 음소거를 해주는게 좋을거라네!"

            if server.id == "81384788765712384" and not server.unavailable: # Discord API
                playground = server.get_channel("94831883505905664") or discord.utils.get(server.channels, name='playground') or server
                await self.safe_send_message(playground, alertmsg.format(uid="98295630480314368")) # fake abal

            elif server.id == "129489631539494912" and not server.unavailable: # Rhino Bot Help
                bot_testing = server.get_channel("134771894292316160") or discord.utils.get(server.channels, name='bot-testing') or server
                await self.safe_send_message(bot_testing, alertmsg.format(uid="98295630480314368")) # also fake abal

        log.debug("%s에 데이터 폴더를 만들고 있다네", server.id)
        pathlib.Path('data/%s/' % server.id).mkdir(exist_ok=True)

    async def on_server_remove(self, server: discord.Server):
        log.info("봇이 서버에서 쫒겨났다네: {}".format(server.name))
        log.debug('서버 목록이 갱신되었다네:')
        [log.debug(' - ' + s.name) for s in self.servers]

        if server.id in self.players:
            self.players.pop(server.id).kill()


    async def on_server_available(self, server: discord.Server):
        if not self.init_ok:
            return # Ignore pre-ready events

        log.debug("서버 \"{}\" 에 접속이 가능해졌다네!".format(server.name))

        player = self.get_player_in(server)

        if player and player.is_paused:
            av_paused = self.server_specific_data[server]['availability_paused']

            if av_paused:
                log.debug("\"{}\" 에 여유가 생겨서 다시 재생을 시작하겠네.".format(server.name))
                self.server_specific_data[server]['availability_paused'] = False
                player.resume()


    async def on_server_unavailable(self, server: discord.Server):
        log.debug("서버 \"{}\" 가 닫힌것 같다네.".format(server.name))

        player = self.get_player_in(server)
       
        if player and player.is_playing:
            log.debug(" \"{}\" 가 혼잡해져서 재생을 멈추겠다네.".format(server.name))
            self.server_specific_data[server]['availability_paused'] = True
            player.pause()
    
    #################################
    #  Custom toots.  yup T O O T S
    ##################################
    async def cmd_처음(self):
        """
        사용법:
            {command_prefix}처음
        별칭:
            {command_prefix}도움2

        반갑네! 자주쓰는 명령어들을 알려줄 것이라네!
        """
        msg = '```\n\n 반갑네!'
        msg += '\n\n 자주 사용하는 명령어들을 알려줄거라네'
        msg += '```\n\n 들어와 -> 계신 음성채팅방으로 들어가게 된다네'
        msg += '\n 시작 -> 음악 검색 및 재생하는데 쓰인다네'
        msg += '\n 재생목록 -> 시작에서 요청한 곡들의 목록이 있다네'
        msg += '\n 돌아가 -> 음성채팅방에서 나가게 된다네!'
        msg += '\n\n 그 외의 명령어들은 {}도움 에 적혀있다네!'.format(self.config.command_prefix)

        return Response(msg, delete_after=10)
    cmd_도움2 = cmd_처음

    async def cmd_안녕(self, author):
        """

        사용법:
            {command_prefix}안녕
        별칭:
            {command_prefix}안냥, {command_prefix}하이
        
        예시:
            {command_prefix}안녕

        안녕하신가! ***!    - 을 말해줄거라네!
        """
        return Response('안녕하신가. %s!'%author, delete_after=10)
    cmd_하이 = cmd_안냥 = cmd_안녕    

    #### 커스텀 Play 명령어!
    async def cmd_시작(self, player, channel, author, message, leftover_args):
        """
        사용법:
            {command_prefix}시작  찾을곡
            {command_prefix}시작  음악주소
        별칭:
            {command_prefix}재생, {command_prefix}틀어, {command_prefix}ㄱ, {command_prefix}가즈아, {command_prefix}가즈아ㅏ,{command_prefix}cmd_가즈아ㅏㅏ

        예시:
            {command_prefix}시작  김동률
            {command_prefix}시작  김동률 출발
            {command_prefix}시작  https://www.youtube.com/watch?v=xgvckGs6xhU

        i) 찾을곡을 질문했을때:
           1. 질문하신 곡에 대한 상위 5가지 검색결과를 알려준다네.
           2. {command_prefix}1 부터 {command_prefix}5 사이의 값을 입력해서 곡을 골라주겠나!

        ii) 음악주소를 줬을때:
           1. 바로 재생될꺼라네!

        추가된 곡은 {command_prefix}재생목록 에서 확인하실수 있을거라네.
        """
        def argcheck():
            if not leftover_args:
                # noinspection PyUnresolvedReferences
                raise exceptions.CommandError(
                    "다음 명령들을 사용해서 정확하게 검색 질문을 해주겠나!\n%s" % dedent(
                        self.cmd_시작.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=20
                )
        argcheck()

        song_url = ''
        await self.send_typing(channel)
        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
        song_url = song_url.strip()
        linksRegex = "((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)"
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        if matchUrl is not None:
            await self.cmd_oldplay(player, channel, author, [], song_url )
            return 
        leftover_args = map(lambda x: x.replace("'", '').replace('"', ''), leftover_args)
        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("검색 질문을 정확하게 해주겠나.", expire_in=30)

        service = 'youtube'
        items_requested = 5
        max_items = 6  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("%s개 이상의 비디오는 동시에 검색을 할수 없다네!" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)
        leftover_args = list(urllib.parse.quote_plus(x) for x in leftover_args)
        search_msg = await self.send_message(channel, "동영상을 찾는중이라네.")
        await self.send_typing(channel)
        try:
            info = await self.downloader.dev_ytbsearch_custom(player.playlist.loop, *leftover_args, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("동영상을 찾지 못했다네.", delete_after=10)
        msg = '\n'
        num = 0
        for tup in info:
            num = num + 1
            msg += str(num) + '  :  '+ tup[1] + ' & 재생시간:' +  tup[2]+ '\n'
          
        result_message = await self.safe_send_message(channel,'%s\n\n결과물이라네!'%msg, delete_after=10)
        res = await self.wait_for_message(author=author, timeout=30)
        
        if not res:
            await self.safe_delete_message(result_message)
            await self.cmd_clean(message, channel, message.server, author, 2)
            return Response("20초가 지났다네. 다시 질문해주겠나!", delete_after=20)
        command = res.content[len(self.config.command_prefix):].lower().strip()
        try:
            int(command)
        except ValueError:
            await self.safe_delete_message(result_message)
            return Response("말한건 {} 부터 {} 사이의 숫자가 아닌것같다네".format('0', str(num)), delete_after=10)
        
        #await self.safe_delete_message(res)
        await self.safe_delete_message(result_message)
        await self.cmd_clean(message, channel, message.server, author)
        newmsg = ''
        if int(command) > 0 and int(command) < num + 1 :
            ytbprefix = 'https://www.youtube.com'
            if res:
                await self.cmd_oldplay(player, channel, author, [], song_url = str(ytbprefix+str(info[int(res.content[len(self.config.command_prefix):].lower().strip())-1][0])))
                newmsg = "바로 시작하겠네!"
            else:
                await self.safe_delete_message(result_message)
                newmsg ="미안하네. \N{SLIGHTLY FROWNING FACE}"
        await self.cmd_queue(channel, player)
        return Response(newmsg, delete_after=15)
    cmd_ㄱ = cmd_가즈아ㅏㅏ = cmd_가즈아ㅏ = cmd_가즈아 = cmd_재생 = cmd_틀어 = cmd_play = cmd_시작

    async def cmd_옥시크린(self, message, channel, server, author, search_range=5):
        """
        *** 경고! 위험한 명령어라네! ***
        사용법:
            {command_prefix}옥시크린
            {command_prefix}옥시크린  [갯수]

        별칭:
            {command_prefix}옥시싹싹, {command_prefix}슈퍼클리너, {command_prefix}슈퍼크리너

        예시:
            {command_prefix}옥시크린  
            {command_prefix}옥시크린  10

        메세지를 가장 최근 메세지부터 차례대로 지워준다네!
        기본 [갯수]는 5개라네. 최대 1000까지 가능은 한데 부담이 심할거라네.

        {command_prefix}청소 보다 강력한 명령어라네.
        이 봇이 말한 메세지 외에 다른 사람들 메세지까지 삭제를 헌더내!

        이 명령어로는 최대 14일 전의 메세지까지 삭제가 가능하다네.
        """
        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("숫자를 입력해주겠나.  숫자를, 10진수로!  `15`같은걸로 말일세.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        if channel.permissions_for(server.me).manage_messages:
            deleted = await self.purge_from(channel, limit=search_range, before=message)
            return Response('{} 개 대화를 삭제했다네!{}'.format(len(deleted), '\n노래관련 명령어만 입력해주겠나!' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('{} 개 대화를 삭제했다네{}.'.format(deleted, ' ' * bool(deleted)), delete_after=6)
    cmd_슈퍼크리너 = cmd_슈퍼클리너 = cmd_옥시싹싹 = cmd_옥시크린
    

    @owner_only
    async def cmd_초강력옥시크린(self, message, channel, server, author, search_range=10):
        """
        
        *** 경고! 위험한 명령어라네! ***
        *** @owner_only 태그를 달아뒀다네! 봇 주인 외에는 사용이 불가할거라네.?

        사용법:
            {command_prefix}초강력옥시크린
            {command_prefix}초강력옥시크린  [갯수]

        별칭:
            {command_prefix}초강력옥시싹싹, {command_prefix}울트라크리너

        예시:
            {command_prefix}초강력옥시크린  
            {command_prefix}초강력옥시크린  20
크
        메세지를 가장 최근 메세지부터 차례대로 지워준다네!
        기본 [갯수]는 10개라네. 최대 1000까지 가능은 한데 부담이 심할거라네.

        {command_prefix}옥시크린 보다 강력한 명령어라네.
        이 명령어로는 날짜 제한 없이 삭제가 가능하다네.
        하지만 일일히 하나하나 지우다보니 시간도 꽤 걸리고 부하도 많이간다네

        사용하는걸 최대한 자제해주겠나.
        """
        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("숫자를 입력해주겠나.  숫자를, 10진수로!  `15`같은걸로 말일세.", reply=True, delete_after=8)
        await self.safe_delete_message(message, quiet=True)

        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id
        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if delete_all or entry.author == author:
                try:
                    await self.delete_message(entry)
                    await asyncio.sleep(0.21)
                    deleted += 1
                except discord.Forbidden:
                    print(discord.Forbidden)
                    pass
                except discord.HTTPException:
                    pass

        return Response('{} 개 대화를 삭제했다네{}.'.format(deleted, ' ' * bool(deleted)), delete_after=6)
    cmd_울트라크리너 = cmd_초강력옥시싹싹 = cmd_초강력옥시크린

    async def cmd_1(self):
        '''
        임시처방이야!
        노래 선택할때 쓰면될꺼야!
        '''
        pass
    cmd_5=cmd_4=cmd_3=cmd_2=cmd_1

    ######
    #긴것들
    ######
    async def cmd_우선시작(self, player, channel, author, message, leftover_args):
        """
        사용법:
            {command_prefix}우선시작  찾을곡
            {command_prefix}우선시작  음악주소
        별칭:
            {command_prefix}우선, {command_prefix}우선재생, {command_prefix}루시우, {command_prefix}빨리

        예시:
            {command_prefix}우선재생  김동률
            {command_prefix}우선재생  김동률 출발
            {command_prefix}우선재생  https://www.youtube.com/watch?v=xgvckGs6xhU

        {command_prefix}시작 명령과 같으나 요청한 곡을 바로 다음곡으로 넣어준다네
        """
        def argcheck():
            if not leftover_args:
                # noinspection PyUnresolvedReferences
                raise exceptions.CommandError(
                    "다음 명령들을 사용해서 정확하게 검색 질문을 해주겠나!\n%s" % dedent(
                        self.cmd_우선시작.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=20
                )
        argcheck()

        song_url = ''
        await self.send_typing(channel)
        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
        song_url = song_url.strip()
        linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        if matchUrl is not None:
            await self.cmd_oldplay(player, channel, author, [], song_url )
            return 
        leftover_args = map(lambda x: x.replace("'", '').replace('"', ''), leftover_args)
        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("검색 질문을 정확하게 해주겠나.", expire_in=30)

        service = 'youtube'
        items_requested = 5
        max_items = 6  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("%s개 이상의 비디오는 동시에 검색을 할수 없다네!" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)
        leftover_args = list(urllib.parse.quote_plus(x) for x in leftover_args)
        search_msg = await self.send_message(channel, "동영상을 찾는중이라네.")
        await self.send_typing(channel)
        try:
            info = await self.downloader.dev_ytbsearch_custom(player.playlist.loop, *leftover_args, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("동영상을 찾지 못했다네.", delete_after=10)
        msg = '\n'
        num = 0
        for tup in info:
            num = num + 1
            msg += str(num) + '  :  '+ tup[1] + ' & 재생시간:' +  tup[2]+ '\n'
          
        result_message = await self.safe_send_message(channel,'%s\n\n결과물이라네!'%msg, delete_after=10)

        res = await self.wait_for_message(author=author, timeout=30)
        
        if not res:
            await self.safe_delete_message(result_message)
            await self.cmd_clean(message, channel, message.server, author, 2)
            return Response("20초가 지났다네. 다시 질문해주겠나!", delete_after=20)
        command = res.content[len(self.config.command_prefix):].lower().strip()

        #await self.safe_delete_message(res)
        await self.safe_delete_message(result_message)
        await self.cmd_clean(message, channel, message.server, author)
        try:
            int(command)
        except ValueError:
            await self.safe_delete_message(result_message)
            return Response("말한건 {} 부터 {} 사이의 숫자가 아닌것같다네".format('0', str(num)), delete_after=10)
        if int(command) > 0 and int(command) < num + 1 :
            ytbprefix = 'https://www.youtube.com'
            song_url = str(ytbprefix+str(info[int(res.content[len(self.config.command_prefix):].lower().strip())-1][0]))
            async with self.aiolocks[_func_() + ':' + author.id]:
                try:
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                except Exception as e:
                    raise exceptions.CommandError(e, expire_in=30)

                if not info:
                    raise exceptions.CommandError(
                        "이 동영상은 실행할수 없는거같다네. {}생방송 명령어를 사용해보겠나!".format(self.config.command_prefix),
                        expire_in=30
                    )

                # abstract the search handling away from the user
                # our ytdl options allow us to use search strings as input urls
                if info.get('url', '').startswith('ytsearch'):
                    # print("[Command:play] Searching for \"%s\"" % song_url)
                    info = await self.downloader.extract_info(
                        player.playlist.loop,
                        song_url,
                        download=False,
                        process=True,    # ASYNC LAMBDAS WHEN
                        on_error=lambda e: asyncio.ensure_future(
                            self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                        retry_on_error=True
                    )

                    if not info:
                        raise exceptions.CommandError(
                            "검색한 것에서 정보를 알아보는데 실패했다네. youtubed1이 아무런 정보도 주지 않은것같네. "
                            "이 문제가 계속되면 재시작해주겠나! ", expire_in=30
                        )

                    if not all(info.get('entries', [])):
                        # empty list, no data
                        log.debug("비어있는 목록만 받았다네. 정보가 없는것같다네.")
                        return

                    # TODO: handle 'webpage_url' being 'ytsearch:.' or extractor type
                    song_url = info['entries'][0]['webpage_url']
                    info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
                    # Now I could just do: return await self.cmd_oldplay(player, channel, author, song_url)
                    # But this is probably fine

                # TODO: Possibly add another check here to see about things like the bandcamp issue
                # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls
                if 'entries' in info:
                    # I have to do exe extra checks anyways because you can request an arbitrary number of search results
                    # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
                    num_songs = sum(1 for _ in info['entries'])

                    if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                        try:
                            return await self._cmd_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                        except exceptions.CommandError:
                            raise
                        except Exception as e:
                            log.error("재생목록에 추가하는데 실패했다네.", exc_info=True)
                            raise exceptions.CommandError("재생목록에 추가 실패:\n%s" % e, expire_in=30)

                    t0 = time.time()

                    # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                    # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                    # I don't think we can hook into it anyways, so this will have to do.
                    # It would probably be a thread to check a few playlists and get the speed from that
                    # Different playlists might download at different speeds though
                    wait_per_song = 1.2

                    procmesg = await self.safe_send_message(
                        channel,
                        '{} 개 곡에 대하여 정보를 모으는중이라네!{}'.format(
                            num_songs,
                            ', 예상시간: {} 초'.format(fixg(
                                num_songs * wait_per_song)) if num_songs >= 10 else '.'))

                    # We don't have a pretty way of doing this yet.  We need either a loop
                    # that sends these every 10 seconds or a nice context manager.
                    await self.send_typing(channel)

                    # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
                    #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

                    entry_list, position = await player.playlist.priority_import_from(song_url, channel=channel, author=author)

                    tnow = time.time()
                    ttime = tnow - t0
                    listlen = len(entry_list)
                    drop_count = 0

                    log.info("{} 개 곡을 {} 초에, {:.2f}초/곡 으로 처리 완료했다네!, {:+.2g}/곡 의 예상시간은 ({}초)".format(
                        listlen,
                        fixg(ttime),
                        ttime / listlen if listlen else 0,
                        ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                        fixg(wait_per_song * num_songs))
                    )

                    await self.safe_delete_message(procmesg)

                    if not listlen - drop_count:
                        raise exceptions.CommandError(
                            "추가된 곡이 없다네! \n모든 곡이 최대 대기시간을 넘어갔다네. ",
                            expire_in=30
                        )

                    reply_text = "**%s** 개 곡을 재생목록에 추가했다네. \n재생목록에서의 현재 위치는 %s번 이라네!"
                    btext = str(listlen - drop_count)

                else:
                    try:
                        entry, position = await player.playlist.add_entry_custom(song_url, channel=channel, author=author)

                    except exceptions.WrongEntryTypeError as e:
                        if e.use_url == song_url:
                            log.warning("목록에는 이상이 있는데. 주신 주소는 정상인거같다네.  \n도와주겠나..")

                        log.debug("주신 주소인 \"%s\" => 단일 곡이 아니라, 노래목록이 아닐까 생각되네" % song_url)
                        log.debug("\"%s\" 를 대신 사용해보겠나!" % e.use_url)

                        return await self.cmd_우선시작(player, channel, author, leftover_args, e.use_url)

                    reply_text = "재생목록에 **%s** 를 추가했다네."
                    btext = entry.title
                    try:
                        time_until = timedelta(seconds=player.current_entry.duration) - timedelta(seconds=player.progress)
                        reply_text += '\n- 재생까지 남은 시간 예상은: %s 이라네!'
                    except:
                        traceback.print_exc()
                        time_until = ''

                    reply_text %= (btext, ftimedelta(time_until))
        await self.cmd_clean(message, channel, message.server, author)
        await self.cmd_queue(channel, player)
        return Response(reply_text)
    cmd_우선 = cmd_우선재생 = cmd_우선시작
