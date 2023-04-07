import asyncio
import json
import logging
import aiohttp
from itertools import islice
from galaxy.api.types import UserInfo
from galaxy.api.errors import (
    UnknownBackendResponse,
    UnknownError,
    BackendError,
)

logger = logging.getLogger(__name__)

class SteamApiClient:
    def __init__(self, http_client):
        self._http_client = http_client

    async def get_data(self, url: str):
        response = await self._http_client.get(url);
        result = await response.text(encoding="utf-8", errors="replace");
        return json.loads(result)

    async def get_profile_data(self, steam_id, api_key):
        url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={}&steamids={}".format(api_key, steam_id)
        profile = await self.get_data(url)
        return profile['response']['players'][0]['personaname']

    async def get_games(self, steam_id, api_key):
        url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/?key={}&steamid={}&format=json&include_appinfo=1&include_played_free_games=1".format(api_key, steam_id)
        games = await self.get_data(url)
        return games['response']['games']

    async def get_achievements(self, steam_id, api_key, game_id):
        achievements = []
        url_all_achievements = "http://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v0002/?key={}&appid={}".format(api_key, game_id)
        all_achievements = await self.get_data(url_all_achievements)
        # return empty list if game has no achievements
        if not 'availableGameStats' in all_achievements['game'] or not 'achievements' in all_achievements['game']['availableGameStats']: 
            return achievements

        url_player_achievements = "http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/?key={}&steamid={}&appid={}".format(api_key, steam_id, game_id)
        player_achievements = await self.get_data(url_player_achievements)
        all_achievements_array = all_achievements['game']['availableGameStats']['achievements']
        player_achievements_array = player_achievements['playerstats']['achievements']
        
        try:
            for p in player_achievements_array:
                unlock_time = int(p['unlocktime'])
                id = p['apiname']
                name = next(a['displayName'] for a in all_achievements_array if a['name']==p['apiname'])
                name = name.strip()
                achievements.append((unlock_time, id, name))
        except (KeyError, ValueError, TypeError):
            logger.exception("Can not parse backend response")
            raise UnknownBackendResponse()

        return achievements

    async def get_friends(self, steam_id, api_key):
        url_friends_list = "http://api.steampowered.com/ISteamUser/GetFriendList/v0001/?key={}&steamid={}&relationship=friend".format(api_key, steam_id)
        friends = await self.get_data(url_friends_list)
        friends_array = friends['friendslist']['friends']
        result = []
        # GetPlayerSummaries() method accepts no more that 100 ids, so we need to loop through friends list in chunks of 100
        stop_condition = True
        while stop_condition:
            hundred = list(islice(friends_array, 0, 100))
            url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/?key={}&steamids={}".format(api_key, ",".join(f['steamid'] for f in hundred))
            profiles = await self.get_data(url)
            try:
                result += [
                    UserInfo(
                        user_id=profile['steamid'],
                        user_name=profile['personaname'],
                        avatar_url=profile['avatar'],
                        profile_url=profile['profileurl'],
                    )
                    for profile in profiles['response']['players']
                ]
            except (KeyError, ValueError, TypeError):
                logger.exception("Can not parse backend response")
                raise UnknownBackendResponse()
            friends_array = list(islice(friends_array, 100))
            stop_condition = len(friends_array) > 100
        
        return result