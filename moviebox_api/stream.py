from typing import Dict, Any, Optional, List
import time
from .client import MovieBoxClient

class MovieBoxStream:
    def __init__(self, client: MovieBoxClient):
        self.client = client
        self.sign_cookie_cache = {} # subject_id: {cookie, expiry}

    def get_play_info_v2(self, subject_id: str, season: Optional[int] = None, episode: Optional[int] = None) -> Dict:
        """Calls the play-info/v2 API endpoint."""
        params = {"subjectId": subject_id}
        if season is not None and season > 0:
            params["se"] = season
        if episode is not None and episode > 0:
            params["ep"] = episode

        return self.client.request(
            "GET",
            "/wefeed-mobile-bff/subject-api/play-info/v2",
            params=params,
            headers={
                "User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)",
                "X-M-Version": "4.0.02"
            }
        )

    def get_resources_v2(self, subject_id: str, season: int = 0, episode: int = 0, page: int = 1, per_page: int = 20) -> Dict:
        """Fetches resource mirrors with isVip=true spoofing."""
        params = {
            "subjectId": subject_id,
            "page": page,
            "perPage": per_page,
            "all": 0,
            "startPosition": 1,
            "endPosition": 1,
            "pagerMode": 2,
            "resolution": 0,
            "isVip": "true"
        }
        if season > 0 or episode > 0:
            params["se"] = season
            params["epFrom"] = episode
            params["epTo"] = episode

        return self.client.request(
            "GET",
            "/wefeed-mobile-bff/subject-api/resource/v2",
            params=params
        )

    def recover_resource_position_v2(self, subject_id: str, resource_id: str, fail_url: str) -> Dict:
        """Calls resource-position/v2 to recover original media link for trap clips."""
        params = {
            "subjectId": subject_id,
            "resourceId": resource_id,
            "failUrl": fail_url,
            "failCode": "404",
            "resourceNum": "0",
            "isVip": "true"
        }
        return self.client.request(
            "GET",
            "/wefeed-mobile-bff/subject-api/resource-position/v2",
            params=params
        )

    def get_play_info(self, subject_id: str, season: int = 1, episode: int = 1, resource_id: Optional[str] = None) -> Dict:
        """Calls the play-info API endpoint."""
        params = {
            "subjectId": subject_id,
            "se": season,
            "ep": episode
        }
        if resource_id:
            params["resourceId"] = resource_id

        return self.client.request(
            "GET",
            "/wefeed-mobile-bff/subject-api/play-info",
            params=params,
            headers={
                "User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)",
                "X-M-Version": "4.0.02"
            }
        )

    def get_stream(self, subject_id: str, auto_refresh: bool = True) -> Dict:
        """
        Resolves the stream URL and the signed cookie for media access.
        
        Returns:
            {
                "url": "...m3u8",
                "headers": {
                    "Cookie": "signCookie=...",
                    "User-Agent": "ExoPlayerLib/2.18.7"
                }
            }
        """
        # Call play-info API
        play_info_res = self.get_play_info(subject_id)
        
        if not play_info_res or "data" not in play_info_res:
            raise ValueError(f"Failed to get play info for {subject_id}: {play_info_res.get('msg', 'Unknown Error')}")

        data = play_info_res["data"]
        
        # Extraction logic based on VideoDetailStreamList model from APK
        video_streams = data.get("streamList", [])
        if not video_streams:
            raise ValueError("No streams found in play info response.")

        # Pick the first available stream (typically highest quality or default)
        stream = video_streams[0]
        stream_url = stream.get("url")
        
        # In MovieBox, authorization relies on a signCookie which is often passed in headers or body
        # According to reverse engineering, it is often assigned in the response.
        sign_cookie = data.get("signCookie", "")
        
        # Cache the cookie if needed for refresh attempts
        self.sign_cookie_cache[subject_id] = {
            "cookie": sign_cookie,
            "timestamp": time.time()
        }

        # Subtitles
        subtitle_list = data.get("subTitleList", [])

        return {
            "url": stream_url,
            "headers": {
                "Cookie": sign_cookie if sign_cookie else "",
                "User-Agent": "ExoPlayerLib/2.18.7" # Mimic default ExoPlayer UA seen in APK
            },
            "subtitles": subtitle_list
        }

    def get_subtitles(self, subject_id: str, lang: str = "en") -> List[Dict]:
        """Convenience method to get subtitles."""
        res = self.get_stream(subject_id)
        return [s for s in res["subtitles"] if s.get("language") == lang or not lang]
