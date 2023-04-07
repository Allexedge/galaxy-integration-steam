import asyncio
import contextlib
import webbrowser
import json
import logging
from typing import Any, Dict, List, Callable, Type
from urllib import parse

from galaxy.api.consts import LicenseType
from galaxy.api.errors import (
    AccessDenied, AuthenticationRequired, UnknownBackendResponse, UnknownError, InvalidCredentials
)
from galaxy.api.jsonrpc import InvalidParams
from galaxy.api.types import (
    Achievement, Authentication, Game, GameTime, LicenseInfo, GameLibrarySettings
)

import achievements_cache
from backend_interface import BackendInterface
from cache import Cache
from http_client import HttpClient
from leveldb_parser import LevelDbParser
from persistent_cache_state import PersistentCacheState
from steam_webapi.authentication import next_step_response, StartUri
from steam_webapi.webapi_client import SteamApiClient
from user_profile import UserProfileChecker, ProfileDoesNotExist, ProfileIsNotPublic, ParseError, \
    NotPublicGameDetailsOrUserHasNoGames

logger = logging.getLogger(__name__)


GAME_DOES_NOT_SUPPORT_LAST_PLAYED_VALUE = 86400


class SteamWebApiBackend(BackendInterface):
    def __init__(
        self,
        *,
        http_client: HttpClient,
        user_profile_checker: UserProfileChecker,
        persistent_storage_state: PersistentCacheState,
        persistent_cache: Dict[str, Any],
        store_credentials: Callable,
    ) -> None:

        self._steam_id = None
        self._api_key = None
        self._miniprofile_id = None
        self._level_db_parser = None

        self._persistent_cache = persistent_cache
        self._persistent_storage_state = persistent_storage_state
        self._store_credentials = store_credentials

        self._client = SteamApiClient(http_client)
        self._user_profile_checker = user_profile_checker

        self._achievements_cache = Cache()
        self._achievements_cache_updated = False
        self._achievements_semaphore = asyncio.Semaphore(20)

        self._authentication_lost = lambda: None

        self._load_cache()
    
    def register_auth_lost_callback(self, callback: Callable):
        self._authentication_lost = callback

    def _load_cache(self):
        achievements_cache_ = self._persistent_cache.get("achievements")
        if achievements_cache_ is not None:
            try:
                achievements_cache_ = json.loads(achievements_cache_)
                self._achievements_cache = achievements_cache.from_dict(achievements_cache_)
            except Exception:
                logger.exception("Cannot deserialize achievements cache")

    async def _do_auth(self, steam_id, api_key):
        self._steam_id = steam_id
        self._api_key = api_key
        self._miniprofile_id = int(steam_id)-76561197960265728 # simple conversion of Steam64 to Steam32
        persona_name = await self._client.get_profile_data(steam_id, api_key)
        return Authentication(
            self._steam_id, 
            persona_name
        )

    async def authenticate(self, stored_credentials=None):
        if not stored_credentials:
            return next_step_response(StartUri.LOGIN)
        steam_id = stored_credentials.get("steam_id")
        api_key = stored_credentials.get("api_key")
        if not steam_id:
            raise InvalidCredentials("No Steam ID in stored credentials")
        if not api_key:
            raise InvalidCredentials("No Steam API key in stored credentials")

        return await self._do_auth(steam_id, api_key)

    async def pass_login_credentials(self, step, credentials, cookies):
        try:
            end_uri = credentials['end_uri']
            parsed_url = parse.urlsplit(end_uri)
            params = parse.parse_qs(parsed_url.query)
            logger.info('params: %s' % params)
        except (KeyError, ValueError):
            raise InvalidParams()
        
        if "open_in_default_browser" in end_uri:
            webbrowser.open(params["link"][0])
            return next_step_response(StartUri.LOGIN)

        steam_id = params['steam_id'][0]
        api_key = params['api_key'][0]

        self._store_credentials({'steam_id': steam_id, 'api_key': api_key})
        auth = await self._do_auth(steam_id, api_key)
        return auth

    @contextlib.asynccontextmanager
    async def _handle_non_public_profile(self, error: Type[Exception]):
        try:
            yield
        except error:
            try:
                await self._user_profile_checker.check_is_public_by_steam_id(self._steam_id)
            except (ProfileIsNotPublic, ProfileDoesNotExist, NotPublicGameDetailsOrUserHasNoGames) as e:
                self._authentication_lost()
                raise AccessDenied(f"Profile not publicly reachable: {e}")
            except ParseError:
                raise UnknownBackendResponse()
            raise

    async def get_owned_games(self):
        if self._steam_id is None or self._api_key is None:
            raise AuthenticationRequired()

        games = await self._client.get_games(self._steam_id, self._api_key)

        try:
            return [
                Game(str(game["appid"]), game["name"], [], LicenseInfo(LicenseType.SinglePurchase, None))
                for game in games
            ]
        except (KeyError, ValueError):
            logger.exception("Cannot parse backend response: %s", games)
            raise UnknownBackendResponse()

    async def prepare_achievements_context(self, game_ids: List[str]) -> Any:
        if self._steam_id is None or self._api_key is None:
            raise AuthenticationRequired()

        return await self._get_game_times_dict()

    async def get_unlocked_achievements(self, game_id: str, context: Any) -> List[Achievement]:
        game_time = await self.get_game_time(game_id, context)

        fingerprint = achievements_cache.Fingerprint(game_time.last_played_time, game_time.time_played)
        achievements = self._achievements_cache.get(game_id, fingerprint)

        if achievements is not None:
            # return from cache
            return achievements

        # fetch from http_client and update cache
        achievements = await self._get_achievements(game_id)
        self._achievements_cache.update(game_id, achievements, fingerprint)
        self._achievements_cache_updated = True
        return achievements

    def achievements_import_complete(self) -> None:
        if self._achievements_cache_updated:
            self._persistent_cache["achievements"] = achievements_cache.as_dict(self._achievements_cache)
            self._persistent_storage_state.modified = True
            self._achievements_cache_updated = False

    async def _get_achievements(self, game_id):
        async with self._achievements_semaphore:
            achievements = await self._client.get_achievements(self._steam_id, self._api_key, game_id)
            return [Achievement(unlock_time, id, name) for unlock_time, id, name in achievements]

    async def prepare_game_times_context(self, game_ids: List[str]) -> Any:
        if self._steam_id is None or self._api_key is None:
            raise AuthenticationRequired()

        return await self._get_game_times_dict()

    async def get_game_time(self, game_id: str, context: Any) -> GameTime:
        game_time = context.get(game_id)
        if game_time is None:
            raise UnknownError("Game {} not owned".format(game_id))
        return game_time

    async def _get_game_times_dict(self) -> Dict[str, GameTime]:
        games = await self._client.get_games(self._steam_id, self._api_key)

        game_times = {}

        try:
            for game in games:
                game_id = str(game["appid"])
                last_played = game["rtime_last_played"]
                if last_played == GAME_DOES_NOT_SUPPORT_LAST_PLAYED_VALUE:
                    last_played = None
                game_times[game_id] = GameTime(
                    game_id,
                    int(game["playtime_forever"]),
                    last_played
                )
        except (KeyError, ValueError):
            logger.exception("Cannot parse backend response")
            raise UnknownBackendResponse()

        return game_times

    async def prepare_game_library_settings_context(self, game_ids: List[str]) -> Any:
        if self._steam_id is None or self._api_key is None:
            raise AuthenticationRequired()

        if not self._level_db_parser:
            self._level_db_parser = LevelDbParser(self._miniprofile_id)

        self._level_db_parser.parse_leveldb()

        if not self._level_db_parser.lvl_db_is_present:
            return None
        else:
            leveldb_static_games_collections_dict = self._level_db_parser.get_static_collections_tags()
            logger.info(f"Leveldb static settings dict {leveldb_static_games_collections_dict}")
            return leveldb_static_games_collections_dict

    async def get_game_library_settings(self, game_id: str, context: Any) -> GameLibrarySettings:
        if not context:
            return GameLibrarySettings(game_id, None, None)
        else:
            game_tags = context.get(game_id)
            if not game_tags:
                return GameLibrarySettings(game_id, [], False)

            hidden = False
            for tag in game_tags:
                if tag.lower() == 'hidden':
                    hidden = True
            if hidden:
                game_tags.remove('hidden')
            return GameLibrarySettings(game_id, game_tags, hidden)

    async def get_friends(self):
        if self._steam_id is None or self._api_key is None:
            raise AuthenticationRequired()

        return await self._client.get_friends(self._steam_id, self._api_key)
