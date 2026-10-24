from fastapi import FastAPI, HTTPException, Query, Response, Cookie, Request
from fastapi.responses import StreamingResponse
import httpx
import requests
import urllib.request
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict
from pydantic import BaseModel
import uvicorn
import os
import subprocess
import base64
import json
import asyncio
import logging
import uuid
import re
from urllib.parse import quote
from moviebox_api import MovieBoxClient, MovieBoxAuth, MovieBoxContent, MovieBoxStream, MovieBoxUser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MovieBox Unofficial API Backend")


@app.get("/")
def root_health():
    return {"status": "ok", "service": "moviebox-internal-api"}


@app.get("/health")
def health_check():
    return {"status": "ok"}

_allowed_origins = [
    origin.strip()
    for raw in (os.getenv("CORS_ORIGINS", ""), os.getenv("FRONTEND_URL", ""))
    for origin in raw.split(",")
    if origin.strip()
]
_allowed_origins += ["http://localhost:3000", "http://localhost:3001", "http://127.0.0.1:3000", "http://127.0.0.1:3001"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(set(_allowed_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sessions: Dict[str, Dict] = {}
HISTORY_FILE = "local_history.json"

def load_local_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load local history: {e}")
    return {"default": [], "blacklist": []}

def save_local_history(h: dict):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Failed to save local history: {e}")

def get_session(session_id: Optional[str] = None):
    if session_id and session_id in sessions:
        return sessions[session_id]
        
    sid = str(uuid.uuid4())
    auth = MovieBoxAuth()
    client = MovieBoxClient(auth=auth)
    sessions[sid] = {
        "id": sid,
        "auth": auth,
        "client": client,
        "content": MovieBoxContent(client),
        "stream": MovieBoxStream(client),
        "user": MovieBoxUser(client)
    }
    logger.info(f"Created new session: {sid}")
    
    try:
        logger.info(f"Bootstrapping guest credentials for session {sid} via visitor-login...")
        login_res = client.visitor_login()
        if login_res.get("status") == "success":
            logger.info(f"Bootstrap guest token success: {auth.token[:30]}... UID: {auth.user_id}")
        else:
            logger.warning(f"visitor-login returned error: {login_res}")
    except Exception as e:
        logger.error(f"Failed to bootstrap guest session: {e}")
        
    return sessions[sid]

class LoginRequest(BaseModel):
    account: str
    password: str
    authType: int = 1

class RegisterRequest(BaseModel):
    account: str
    password: str
    otp: str
    authType: int = 1

class OtpRequest(BaseModel):
    account: str
    authType: int = 1
    type: int = 1 

@app.post("/request-otp")
def request_otp(req: OtpRequest, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request_otp(req.account, req.authType, req.type)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/login")
def login(req: LoginRequest, response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].login(req.account, req.password, req.authType)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    
    response.set_cookie(
        key="session_id", 
        value=s["id"], 
        httponly=True, 
        samesite="lax",
        max_age=3600 * 24 * 30 
    )
    return res

@app.post("/register")
def register(req: RegisterRequest, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].register(req.account, req.password, req.otp, req.authType)
    if res["status"] == "error":
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/logout")
def logout(response: Response, session_id: Optional[str] = Cookie(None)):
    if session_id in sessions:
        sessions[session_id]["client"].logout()
        del sessions[session_id]
    response.delete_cookie("session_id")
    return {"status": "success"}

@app.get("/user-info")
def get_user_info(response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if not session_id:
        response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")

    if not s["auth"].is_logged_in:
        return {"logged_in": False, "mode": "Guest Access", "session_id": s["id"]}
        
    return {
        "logged_in": True,
        "mode": "Official Account", 
        "user": s["auth"].user_info,
        "session_id": s["id"]
    }

def map_actor(actor: dict):
    avatar = actor.get("avatarUrl") or actor.get("avatar") or actor.get("photo") or actor.get("poster") or ""
    if isinstance(avatar, dict): avatar = avatar.get("url") or ""
    if isinstance(avatar, str) and avatar.startswith("//"): avatar = "https:" + avatar
    return {
        "name": actor.get("name") or actor.get("actorName") or "Unknown",
        "role": actor.get("character") or actor.get("role") or "Cast",
        "avatar": avatar
    }

def map_room(src: dict):
    return {
        "id": str(src.get("groupId") or src.get("id")),
        "name": src.get("name") or "Community Room",
        "title": src.get("name") or "Community Room",
        "avatar": src.get("cover", {}).get("url") if isinstance(src.get("cover"), dict) else src.get("avatar") or "",
        "description": src.get("description") or "",
        "members": src.get("userCount") or 0,
        "posts": src.get("postCount") or 0,
        "tags": src.get("tags") or []
    }

def map_item(src: dict, depth: int = 0):
    if "subject" in src and isinstance(src["subject"], dict):
        item = src["subject"]
    else:
        item = src

    sid = str(item.get("subjectId") or item.get("id") or "")
    title = (
        item.get("title") or item.get("name") or item.get("subjectName") or item.get("subject_name") or
        item.get("categoryName") or item.get("content") or item.get("keyword") or item.get("keywordName") or
        item.get("itemName") or item.get("show_name") or item.get("showTitle") or item.get("titleName") or
        item.get("title_en") or item.get("tag") or item.get("label") or item.get("extra") or
        item.get("subtitle") or item.get("tabName") or item.get("tab_name") or item.get("searchName") or
        item.get("promotionName") or src.get("title") or src.get("name") or src.get("content") or
        src.get("keyword") or src.get("label") or "Unknown"
    )
    
    dlink = str(item.get("deepLink") or src.get("deepLink") or "")
    action_type = "movie"
    category_id = None
    
    if dlink:
        if "/home/category" in dlink:
            action_type = "category"
            if "categoryType=" in dlink:
                category_id = dlink.split("categoryType=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = category_id
        elif "/playlist/detail" in dlink:
            action_type = "playlist"
            if "playlistId=" in dlink:
                extracted_id = dlink.split("playlistId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/movie/detail" in dlink:
            action_type = "movie"
            if "subjectId=" in dlink:
                extracted_id = dlink.split("subjectId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/actor/" in dlink:
            action_type = "actor"
            if "actorId=" in dlink:
                extracted_id = dlink.split("actorId=")[1].split("&")[0]
                if not sid or sid == "0":
                    sid = extracted_id
        elif "/post/" in dlink or "detailVideo" in dlink:
            action_type = "post"
            if "id=" in dlink:
                extracted_id = dlink.split("id=")[1].split("&")[0]
                if sid == "0" or not sid:
                    sid = extracted_id

    poster = item.get("poster")
    poster_url = ""
    if isinstance(poster, dict): poster_url = poster.get("url")
    elif isinstance(poster, str): poster_url = poster
    
    if not poster_url:
        cover = item.get("cover")
        poster_url = cover.get("url") if isinstance(cover, dict) else cover

    if not poster_url:
        img_terms = ["image", "img", "thumb", "thumbnail", "poster", "cover", "icon", "banner", "pic", "picture"]
        for term in img_terms:
            val = item.get(term)
            if isinstance(val, dict) and val.get("url"):
                poster_url = val.get("url")
                break
            elif isinstance(val, str) and (val.startswith("http") or val.startswith("//")):
                poster_url = val
                break
        
        if not poster_url:
            for k, v in item.items():
                if any(t in k.lower() for t in img_terms) and isinstance(v, str) and (v.startswith("http") or v.startswith("//")):
                    poster_url = v
                    break

    if not poster_url:
        hp = item.get("horizontalPoster") or item.get("horizontalCover")
        poster_url = hp.get("url") if isinstance(hp, dict) else hp

    if not poster_url:
        banner = item.get("banner")
        if isinstance(banner, dict):
            poster_url = banner.get("image", {}).get("url") or banner.get("url")

    if isinstance(poster_url, str) and poster_url.startswith("//"):
        poster_url = "https:" + poster_url

    score = item.get("imdbRatingValue") or item.get("imdbRate") or item.get("starRating") or item.get("score") or "N/A"
    release_date = item.get("releaseDate") or item.get("releaseTime") or item.get("year") or ""
    display_year = release_date[:4] if release_date and len(release_date) >= 4 else "N/A"

    runtime = item.get("duration") or item.get("runtime") or item.get("minute")
    if isinstance(runtime, int): runtime = f"{runtime}m"

    return {
        "subjectId": sid,
        "id": sid,
        "title": title,
        "cover": poster_url,
        "poster": poster_url,
        "score": str(score),
        "releaseTime": display_year,
        "subjectType": item.get("subjectType") or item.get("type") or item.get("subject_type") or (2 if item.get("episodeCount") or item.get("seasonCount") else 1),
        "runtime": runtime,
        "season": item.get("season"),
        "episode": item.get("episode") or item.get("ep"),
        "seeTime": item.get("seeTime"),
        "seenStatus": item.get("seenStatus"),
        "likeStatus": 1 if (
            item.get("isFavorite") == 1 or item.get("is_favorite") == 1 or item.get("fav") == 1 or
            item.get("is_fav") == 1 or item.get("isLike") == 1 or item.get("is_like") == 1 or
            item.get("wantToSee") == 1 or item.get("likeStatus") == 1 or item.get("collected") == 1 or
            item.get("isCollect") == 1 or item.get("collectedStatus") == 1 or str(item.get("likeType")) == "1" or
            item.get("isCollect") is True or item.get("isFavorite") is True
        ) else 0,
        "description": item.get("description") or "",
        "actionType": action_type,
        "categoryId": category_id,
        "deepLink": dlink,
        "isPost": action_type == "post",
        "streamUrl": item.get("playUrl", {}).get("playUrl") if isinstance(item.get("playUrl"), dict) else None,
        "isCollection": item.get("isCollection", False),
        "items": item.get("items", [])
    }

@app.get("/home")
def get_home(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=1, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Home error: {e}")
        return {"code": 500, "message": str(e), "data": {"list": []}}

@app.get("/anime")
def get_anime(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=8, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Anime error: {e}")
        return {"code": 1, "data": []}

@app.get("/rankings")
def get_rankings(response: Response, tabId: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    variants = ["/wefeed-mobile-bff/tab/ranking-list", "/tab/ranking-list", "/subject-api/ranking-list"]
    
    for v in variants:
        try:
            res = s["content"].get_rankings(v, tab_id=tabId)
            logger.info(f"RANKINGS RAW ({v}): {json.dumps(res)}")
            data = res.get("data")
            if not data: continue
            
            formatted = []
            if "subjects" in data and isinstance(data["subjects"], list):
                items = data["subjects"]
                if items:
                    formatted.append({"title": "Top Rankings", "items": [map_item(i) for i in items[:10]]})
            else:
                lists = data.get("lists") or data.get("list") 
                if isinstance(lists, list):
                    for l in lists:
                        if not isinstance(l, dict): continue
                        title = l.get("name") or l.get("title") or "Rankings"
                        items = l.get("items") or l.get("list") or []
                        if items:
                            formatted.append({"title": title, "items": [map_item(i) for i in items[:10]]})
            
            if formatted: return {"code": 0, "data": formatted}
        except Exception as e:
            logger.error(f"Error parsing rankings variant {v}: {e}")
            continue
        
    return {"code": 0, "data": []}

@app.get("/discovery")
def get_discovery(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_discovery()
        logger.info(f"DISCOVERY RAW: {json.dumps(res)[:1000]}")
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or []
        return {"code": 0, "data": [map_item(i) for i in items[:20]]}
    except Exception as e:
        logger.error(f"Discovery error: {e}")
        return {"code": 1, "data": []}

def _extract_items(value, depth: int = 0) -> list:
    if value is None or depth > 6:
        return []
    if isinstance(value, list):
        flattened = []
        for item in value:
            if isinstance(item, dict) and any(isinstance(item.get(k), (list, dict)) for k in ("items", "list", "subjects", "movieList", "data")):
                nested = next((item.get(k) for k in ("items", "list", "subjects", "movieList", "data") if item.get(k) is not None), None)
                flattened.extend(_extract_items(nested, depth + 1) or [item])
            else:
                flattened.append(item)
        return flattened
    if isinstance(value, dict):
        for key in ("items", "list", "subjects", "movieList", "results", "data"):
            if key in value:
                items = _extract_items(value[key], depth + 1)
                if items:
                    return items
    return []

@app.get("/trending")
def get_trending(response: Response, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        candidates = [
            s["content"].get_trending(),
            s["content"].get_home_list(category_id=1),
            s["content"].get_categories(category_id=1, page=1),
        ]
        for res in candidates:
            logger.info("TRENDING RAW: %s", json.dumps(res, default=str)[:1200])
            items = _extract_items(res)
            mapped = [map_item(i) for i in items[:40] if isinstance(i, dict)]
            if mapped:
                return {"code": 0, "data": mapped}
        return {"code": 0, "data": [], "meta": {"source": "moviebox", "empty": True}}
    except Exception as e:
        logger.exception("Trending upstream error")
        raise HTTPException(status_code=502, detail=f"MovieBox trending unavailable: {e}")

@app.get("/movies")
def get_movies(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=2, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.exception("Movies upstream error")
        raise HTTPException(status_code=502, detail=f"MovieBox movies unavailable: {e}")

def format_tab_sections(items: list):
    sections = []
    is_direct_movies = True
    for row in items:
        if isinstance(row, dict) and (row.get("list") or row.get("items") or row.get("subjects") or row.get("movieList") or row.get("customData") or row.get("banner")):
            is_direct_movies = False
            break
            
    if is_direct_movies and items:
         mapped = [map_item(m) for m in items if m.get("subjectId") or m.get("id")]
         if mapped: return [{"title": "Content", "items": mapped}]

    for row in items:
        if not isinstance(row, dict): continue
        title = row.get("title") or row.get("name") or "Section"
        
        inner = []
        if isinstance(row.get("list"), list) and row.get("list"): inner = row.get("list")
        elif isinstance(row.get("items"), list) and row.get("items"): inner = row.get("items")
        elif isinstance(row.get("subjects"), list) and row.get("subjects"): inner = row.get("subjects")
        elif isinstance(row.get("movieList"), list) and row.get("movieList"): inner = row.get("movieList")
        elif isinstance(row.get("customData"), dict) and isinstance(row["customData"].get("items"), list) and row["customData"]["items"]:
            inner = row["customData"]["items"]
        elif isinstance(row.get("banner"), dict) and isinstance(row["banner"].get("banners"), list) and row["banner"]["banners"]:
            inner = row["banner"]["banners"]
            
        real_movies = []
        for i in inner:
            if not isinstance(i, dict): continue
            if isinstance(i.get("subject"), dict):
                 real_movies.append(i["subject"])
            elif i.get("subjectId") or i.get("id"):
                 real_movies.append(i)
        
        if real_movies:
            mapped = [map_item(m) for m in real_movies]
            sections.append({
                "title": title,
                "type": row.get("subjectType") or row.get("type") or "SUBJECTS_MOVIE",
                "items": mapped
            })
    return sections

@app.get("/short-tv")
def get_short_tv(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=13, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"ShortTV error: {e}")
        return {"code": 1, "data": []}

@app.get("/kids")
def get_kids(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=23, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Kids error: {e}")
        return {"code": 1, "data": []}

@app.get("/education")
def get_education(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=3, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Education error: {e}")
        return {"code": 1, "data": []}

@app.get("/music")
def get_music(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=4, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Music error: {e}")
        return {"code": 1, "data": []}

@app.get("/asian")
def get_asian(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=18, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Asian error: {e}")
        return {"code": 1, "data": []}

@app.get("/western")
def get_western(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=19, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Western error: {e}")
        return {"code": 1, "data": []}

@app.get("/nollywood")
def get_nollywood(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=28, page=page)
        if not isinstance(res, dict):
             return {"code": 1, "message": "Invalid response format", "data": {"list": []}}
        
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        
        if items:
            sample = items[0].get('name') or items[0].get('title') or "Unknown"
            logger.info(f"Nollywood Detection: First item is '{sample}'")
            
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Nollywood error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}

@app.get("/tab/{tab_id}")
def get_tab_generic(tab_id: int, page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=tab_id, page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Generic Tab {tab_id} error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}

@app.get("/game")
def get_game(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_categories(category_id=11, page=page)
        if not isinstance(res, dict):
             return {"code": 1, "message": "Invalid response format", "data": {"list": []}}
             
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or data.get("subjects") or []
        
        if items:
            sample = items[0].get('name') or items[0].get('title') or "Unknown"
            logger.info(f"Game Detection: First item is '{sample}'")
            
        return {"code": 0, "data": {"list": format_tab_sections(items)}}
    except Exception as e:
        logger.error(f"Game error: {e}")
        return {"code": 1, "message": str(e), "data": {"list": []}}

@app.get("/search-suggestions")
def get_search_suggestions(response: Response, q: Optional[str] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        if q:
            res = s["content"].search(q, page=1)
            data = res.get("data", {})
            items = data.get("list") or data.get("items") or data.get("movie") or data.get("subjects") or []
        else:
            res = s["content"].get_search_suggestions()
            logger.info(f"SEARCH SUGGESTIONS RAW: {json.dumps(res)}")
            data = res.get("data") if isinstance(res, dict) else {}
            if not isinstance(data, dict):
                return {"code": 0, "data": []}
            items = data.get("list") or data.get("items") or data.get("movie") or data.get("subjects") or []
            
        suggestions = []
        for i in items:
            if isinstance(i, str):
                suggestions.append(i)
            elif isinstance(i, dict):
                suggestions.append(i.get("keyword") or i.get("title") or i.get("name"))
        
        return {"code": 0, "data": [s for s in suggestions if s]}
    except Exception as e:
        logger.error(f"Search suggestions error: {e}")
        return {"code": 0, "data": []}

@app.get("/search")
def search(response: Response, q: str, page: int = 1, subject_type: int = 0, type: Optional[int] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    response.set_cookie(key="session_id", value=s["id"], httponly=True, samesite="lax")
    try:
        if not q.strip():
            return {"code": 0, "data": {"items": []}}
        effective_type = type if type is not None else subject_type
        res = s["content"].search(q.strip(), page=page, subject_type=effective_type)
        logger.info(f"SEARCH RAW ({q}): {json.dumps(res, default=str)[:2000]}")
        items = _extract_items(res)
        return {"code": 0, "data": {"items": [map_item(i) for i in items[:40] if isinstance(i, dict)]}}
    except Exception as e:
        logger.exception("Search upstream error for %s", q)
        raise HTTPException(status_code=502, detail=f"MovieBox search unavailable: {e}")

@app.get("/rooms/recommend")
def get_rooms(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_rooms(page=page)
        data = res.get("data") or {}
        items = data.get("list") or data.get("items") or []
        return {"code": 0, "data": [map_room(r) for r in items]}
    except Exception as e:
        logger.error(f"Rooms error: {e}")
        return {"code": 1, "data": []}

@app.get("/rooms/{room_id}")
def get_room_detail(room_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["content"].get_room_detail(room_id)
        data = res.get("data") or {}
        return {"code": 0, "data": map_room(data)}
    except Exception as e:
        logger.error(f"Room detail error: {e}")
        return {"code": 1, "data": {}}

@app.get("/detail/{subject_id}")
def get_detail(subject_id: str, depth: int = 0, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["content"].get_movie_detail(subject_id)
    data = res.get("data", {})
    
    is_collection = False
    items = []

    if not data or not (data.get("title") or data.get("name")):
        try:
           post_res = s["client"].request("GET", "/wefeed-mobile-bff/post/get", params={"postId": subject_id})
           post_data = post_res.get("data", {})
           if post_data and post_data.get("postId"):
               media = post_data.get("media") or {}
               video_url = ""
               if media.get("mediaType") == "VIDEO":
                   video_list = media.get("video") or []
                   if video_list and len(video_list) > 0:
                       video_url = video_list[0].get("url") or video_list[0].get("playUrl") or ""
                   elif media.get("url"):
                       video_url = media.get("url")
                       
               data = {
                   "subjectId": subject_id,
                   "title": "Community Post",
                   "description": post_data.get("content") or post_data.get("text") or "",
                   "isPost": True,
                   "streamUrl": video_url,
                   "poster": media.get("cover", {}).get("url") if isinstance(media.get("cover"), dict) else None,
                   "source": f"@{post_data.get('user', {}).get('nickname', 'User')}"
               }
        except: pass

        if not data or not (data.get("title") or data.get("name")):
            try:
               cat_res = s["client"].request(
                   "POST",
                   "/wefeed-mobile-bff/subject-api/genre-top",
                   data={"type": subject_id, "page": 1, "perPage": 50}
               )
               cat_data = cat_res.get("data", {})
               desc = cat_data.get("desc") or "Curated Collection"
               items = cat_data.get("list") or cat_data.get("items") or cat_data.get("subjects") or []
               
               raw_items = [i.get("info") if isinstance(i, dict) and i.get("info") else i for i in items]
               mapped_items = [map_item(m) for m in raw_items]
               if mapped_items:
                   items = mapped_items
                   is_collection = True
                   data = {
                       "subjectId": subject_id,
                       "title": desc,
                       "isCollection": True,
                       "items": mapped_items
                   }
            except: pass

    if not data: return {"code": 1, "msg": "Not found"}
    
    is_fav = False
    if not s["auth"].is_guest_mode:
        try:
            wl_res = s["user"].get_watchlist(page=1, per_page=50)
            wl_items = wl_res.get("data", {}).get("items") or wl_res.get("data", {}).get("list") or []
            for item in wl_items:
                if str(item.get("subject_id") or item.get("id") or item.get("subjectId")) == str(subject_id):
                    is_fav = True
                    break
        except: pass

    if is_fav:
        data["isFavorite"] = 1

    status_fields = ["isFavorite", "is_favorite", "fav", "is_fav", "collected", "isLike", "wantToSee", "likeStatus"]
    for f in status_fields:
        if f in res and f not in data:
            data[f] = res[f]

    try:
        post_res = s["client"].request("GET", "/wefeed-mobile-bff/post/count/subject", params={"subjectId": subject_id})
        data["postCount"] = post_res.get("data", {}).get("count") or "0"
    except:
        data["postCount"] = "0"

    mapped = map_item(data, depth=depth)
    mapped["postCount"] = data.get("postCount", "0")
    mapped["isCollection"] = is_collection
    
    if data.get("isPost"):
        mapped["isPost"] = True
        mapped["streamUrl"] = data.get("streamUrl")
        mapped["source"] = data.get("source")
    
    if is_collection and depth == 0:
        mapped["collectionItems"] = [map_item(i, depth=depth+1) for i in items[:24]]
        if items and not mapped.get("poster"):
            first = map_item(items[0], depth=depth+1)
            mapped["poster"] = first.get("poster")
            mapped["cover"] = first.get("cover")

    raw_cast = data.get("staffList") or data.get("actorList") or []
    mapped["cast"] = [map_actor(a) for a in raw_cast]
    
    all_languages = []
    raw_dubs = data.get("dubs") or []
    for dub in raw_dubs:
        all_languages.append({
            "id": None, 
            "subjectId": dub.get("subjectId"), 
            "name": dub.get("lanName") or "Custom Dub",
            "type": "dub"
        })
        
    try:
        det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id})
        detectors = (det_res.get('data') or {}).get('resourceDetectors') or []
        for d in detectors:
            d_name = d.get("name") or "Resource"
            all_languages.append({
                "id": d.get("resourceId"),
                "subjectId": subject_id, 
                "name": d_name,
                "type": "resource"
            })
    except:
        pass
        
    mapped["languages"] = all_languages
    return {"code": 0, "data": mapped}

@app.get("/episodes/{series_id}")
def get_episodes(series_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["content"].get_episode_list(series_id)
    logger.info(f"EPISODES CLOUD RAW: {json.dumps(res)[:2000]}")
    data = res.get("data") or {}
    raw_seasons = data.get("seasons") or res.get("seasons") or []
    if not raw_seasons:
        raw_seasons = data.get("seasonList") or data.get("list") or []
        
    mapped_seasons = []
    for s_raw in raw_seasons:
        num = s_raw.get("se") or s_raw.get("seasonNumber") or 1
        eps = []
        
        for key in ["allEp", "epList", "episodeList", "episodes", "list", "items"]:
            pool = s_raw.get(key)
            if not pool: continue
            
            if isinstance(pool, str):
                for e_num in pool.split(","):
                    if e_num: eps.append({"episodeNumber": e_num, "title": f"Episode {e_num}", "id": f"{series_id}_{num}_{e_num}"})
            elif isinstance(pool, list):
                for item in pool:
                    if isinstance(item, dict):
                        en = item.get("ep") or item.get("episodeNumber") or item.get("episode_number")
                        if en: eps.append({"episodeNumber": str(en), "title": item.get("title") or f"Episode {en}", "id": f"{series_id}_{num}_{en}"})
                    else:
                        eps.append({"episodeNumber": str(item), "title": f"Episode {item}", "id": f"{series_id}_{num}_{item}"})
            
            if eps: break

        if not eps:
            max_ep = s_raw.get("maxEp") or s_raw.get("max_ep") or 0
            if isinstance(max_ep, str) and max_ep.isdigit(): max_ep = int(max_ep)
            if max_ep and isinstance(max_ep, int):
                for i in range(1, max_ep + 1):
                    eps.append({"episodeNumber": str(i), "title": f"Episode {i}", "id": f"{series_id}_{num}_{i}"})

        if eps:
            mapped_seasons.append({"seasonNumber": num, "episodes": eps})
    return {"code": 0, "data": {"seasons": mapped_seasons}}

@app.get("/stream/{subject_id}")
def get_stream(subject_id: str, season: int = 1, episode: int = 1, quality: Optional[str] = "720p", resource_id: Optional[str] = None, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        subject_detail = s["content"].get_movie_detail(subject_id).get("data") or {}
        is_movie = (str(subject_detail.get("subjectType") or subject_detail.get("type")) == "1")
    except: is_movie = False

    logger.info(f"RESOLVING STREAM: ID={subject_id} Type={'Movie' if is_movie else 'Series'} Resource={resource_id} Quality={quality}")
    
    res_se = None if is_movie else season
    res_ep = None if is_movie else episode
    
    def is_deprecation_notice_url(u: str) -> bool:
        if not u: return False
        lower = u.lower()
        return (
            "notice.mp4" in lower
            or ("macdn.aoneroom.com" in lower and "/other/" in lower)
            or "1c7de0bd3393702d9191801f15f88f8d" in lower
            or "9a0461bc39da389663bf3dbb17091d3f" in lower
            or "80da52722ee7de7a4303290af6783424" in lower
            or "b164fbfb4347792950bdfbfb563d39d9" in lower
        )

    res = s["stream"].get_play_info_v2(subject_id, season=res_se, episode=res_ep)
    data = res.get("data", {}) if isinstance(res, dict) else {}
    streams = data.get("streams") or data.get("streamList") or []
    
    if not streams:
        logger.info("play-info/v2 empty, falling back to legacy play-info...")
        res = s["stream"].get_play_info(subject_id, season=res_se, episode=res_ep, resource_id=resource_id)
        data = res.get("data", {}) if isinstance(res, dict) else {}
        streams = data.get("streamList") or data.get("streams") or []
    
    logger.info(f"PLAY-INFO RESPONSE: code={res.get('code')}, found {len(streams)} streams")

    try:
        r2_se = 0 if is_movie else season
        r2_ep = 0 if is_movie else episode
        res2 = s["stream"].get_resources_v2(subject_id, season=r2_se, episode=r2_ep)
        r2_items = res2.get("data", {}).get("list") or []
        for r_item in r2_items:
            r_link = r_item.get("resourceLink") or r_item.get("downloadUrl") or r_item.get("url")
            if r_link:
                streams.append({
                    "url": r_link,
                    "quality": f"{r_item.get('resolution')}p" if r_item.get('resolution') else "Auto",
                    "signCookie": r_item.get("signCookie") or "",
                    "codec": r_item.get("codecName") or "",
                    "id": str(r_item.get("resourceId") or ""),
                    "duration": r_item.get("duration") or 0
                })
        if r2_items:
            logger.info(f"Appended {len(r2_items)} mirror resources from /resource/v2 (isVip=true)")
    except Exception as e:
        logger.warning(f"Failed to fetch /resource/v2 mirrors: {e}")

    recovered_streams = []
    for _st in streams:
        st_url = _st.get("url", "")
        st_rid = str(_st.get("id") or "")
        if is_deprecation_notice_url(st_url) and st_rid:
            logger.info(f"Detected anti-piracy dummy video clip for resource {st_rid}. Engaging /resource-position/v2 failover...")
            try:
                rec_res = s["stream"].recover_resource_position_v2(subject_id, st_rid, st_url)
                rec_list = rec_res.get("data", {}).get("list") or []
                if rec_list:
                    rec_item = rec_list[0]
                    real_link = rec_item.get("resourceLink") or rec_item.get("url")
                    if real_link and not is_deprecation_notice_url(real_link):
                        logger.info(f"FAILOVER SUCCESS: Recovered real stream link: {real_link[:60]}...")
                        recovered_streams.append({
                            "url": real_link,
                            "quality": f"{rec_item.get('resolution')}p" if rec_item.get('resolution') else _st.get("quality", "Auto"),
                            "signCookie": rec_item.get("signCookie") or _st.get("signCookie") or "",
                            "codec": rec_item.get("codecName") or _st.get("codec", ""),
                            "id": str(rec_item.get("resourceId") or st_rid),
                            "duration": rec_item.get("duration") or _st.get("duration", 0)
                        })
            except Exception as re_err:
                logger.warning(f"Recovery via /resource-position/v2 failed: {re_err}")
        elif not is_deprecation_notice_url(st_url):
            recovered_streams.append(_st)

    if recovered_streams:
        streams = recovered_streams

    if not streams:
        logger.info(f"No streams yet. Checking resourceDetectors for subject {subject_id}...")
        try:
            detectors = subject_detail.get("resourceDetectors") or []
            for det in detectors:
                if resource_id and str(det.get("resourceId")) != str(resource_id):
                    continue
                res_list = det.get("resolutionList") or []
                for res_item in res_list:
                    if not is_movie:
                        item_se = res_item.get("se") or res_item.get("season")
                        item_ep = res_item.get("ep") or res_item.get("episode")
                        if item_se is not None and item_ep is not None:
                            if int(item_se) != int(season) or int(item_ep) != int(episode):
                                continue
                    stream_url = res_item.get("resourceLink") or res_item.get("downloadUrl")
                    if stream_url and not is_deprecation_notice_url(stream_url):
                        streams.append({
                            "url": stream_url,
                            "quality": f"{res_item.get('resolution')}p" if res_item.get("resolution") else "Auto",
                            "signCookie": det.get("signCookie") or res_item.get("signCookie") or "",
                            "codec": res_item.get("codecName") or det.get("codecName") or "",
                            "id": res_item.get("resourceId") or det.get("resourceId") or "",
                            "duration": res_item.get("duration") or det.get("duration") or 0
                        })
            if streams:
                logger.info(f"Recovered {len(streams)} streams directly from details resourceDetectors.")
        except Exception as e:
            logger.error(f"Error extracting from resourceDetectors: {e}")
            
    global_cookie = res.get("signCookie") or data.get("signCookie") or s["client"].session.cookies.get("signCookie") or s["auth"].token
    logger.info(f"Phase 1 - Primary Result: {len(streams)} streams found (code: {res.get('code')}) (Cookie: {'YES' if global_cookie else 'NO'})")
    
    # ⚠️ NOUVEAU : FILTRE QUALITÉ — garde uniquement la qualité demandée
    if quality and quality != "auto":
        q_wanted = str(quality).replace("p", "").replace("P", "").strip()
        if q_wanted.isdigit():
            q_num = int(q_wanted)
            def _qnum(st):
                q = str(st.get("quality", "0")).replace("p", "").replace("P", "").strip()
                try: return int(q) if q.isdigit() else 0
                except: return 0
            filtered = [st for st in streams if _qnum(st) == q_num]
            if filtered:
                logger.info(f"QUALITY FILTER: {q_wanted}p → {len(filtered)} streams retenus (sur {len(streams)})")
                streams = filtered
            else:
                available_q = sorted(set([_qnum(st) for st in streams if _qnum(st) > 0]), reverse=True)
                logger.warning(f"QUALITY FILTER: aucune {q_wanted}p. Disponibles: {available_q}. On garde tout.")
    # ⚠️ FIN NOUVEAU
    
    import requests
    
    working_stream = None
    working_cookie = None
    official_quality = quality.lower() if quality else "720p"
    
    def prioritize_h264(st):
        u = st.get("url", "").lower()
        if "h265" in u or "x265" in u or "hev1" in u: return 10
        if ".mp4" in u: return 0
        if ".m3u8" in u: return 1
        return 5
    
    prioritized_streams = sorted(streams, key=prioritize_h264)

    for st in prioritized_streams:
        url = st.get("url")
        if not url: continue
        if any(bad in url.lower() for bad in ["h265", "x265", "hev1"]):
            continue
        cookie = st.get("signCookie") or global_cookie or ""
        try:
            head_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=False)
            if head_res.status_code in [200, 206, 302]:
                working_stream = st
                working_cookie = cookie
                break
        except: continue

    if not working_stream:
        for st in prioritized_streams:
            url = st.get("url")
            if not url: continue
            cookie = st.get("signCookie") or global_cookie or ""
            try:
                head_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=False)
                if head_res.status_code in [200, 206, 302]:
                    logger.info(f"Using fallback H265/HEVC stream: {url}")
                    working_stream = st
                    working_cookie = cookie
                    break
            except: continue
    subtitles_source = data.get("subTitleList", [])
    
    if not working_stream:
        logger.info(f"Primary Cloud Offline for {subject_id}. Engaing Resource Mirror Rotation...")
        try:
            hdrs = {"User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)", "X-M-Version": "4.0.02"}
            r_ids = []
            try:
                det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=hdrs)
                logger.info(f"Metadata Probe Result: {det_res.get('code')} - {len(det_res.get('data', {}).get('resourceDetectors', []))} detectors found")
                detectors = (det_res.get('data') or {}).get('resourceDetectors') or []
                for d in detectors:
                    if d.get('resourceId'): r_ids.append(d.get('resourceId'))
            except Exception as ex: 
                logger.error(f"Metadata Probe Crash: {ex}")

            if not r_ids:
                try:
                    res_list = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/see-list-v2', params={'subjectId': subject_id, 'page': 1, 'pageSize': 20, 'seeType': 1}, headers=hdrs)
                    logger.info(f"See-List Probe Result: {res_list.get('code')}")
                    r_items = (res_list.get('data') or {}).get('items') or []
                    for item in r_items:
                        if item.get('id'): r_ids.append(item.get('id'))
                except: pass

            for r_id in r_ids:
                logger.info(f"Probing Resource: {r_id} for {subject_id}")
                p_params = {'subjectId': subject_id, 'resourceId': r_id}
                if not is_movie:
                    p_params.update({'se': season, 'ep': episode, 'season': season, 'episode': episode})
                
                q_params = p_params.copy()
                q_params['quality'] = official_quality
                p_info = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/play-info', params=q_params, headers=hdrs)
                p_data = p_info.get('data') or {}
                p_streams = p_data.get('streamList') or p_data.get('streams') or []
                
                if not p_streams:
                    logger.info(f"Retrying probe {r_id} without quality constraint...")
                    p_info = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/play-info', params=p_params, headers=hdrs)
                    p_data = p_info.get('data') or {}
                    p_streams = p_data.get('streamList') or p_data.get('streams') or []

                logger.info(f"Probe {r_id} Result: {len(p_streams)} streams recovered")
                if p_streams:
                    working_stream = p_streams[0]
                    
                    if "hakunaymatata" in working_stream.get("url", ""):
                        try:
                            logger.info(f"Ping session for CDN handshake: {working_stream.get('url')[:60]}")
                            requests.head(working_stream.get("url"), headers={"User-Agent": "ExoPlayerLib/2.19.1"}, timeout=3, cookies=s["client"].session.cookies)
                        except: pass

                    working_cookie = (
                        p_info.get('signCookie') or 
                        p_data.get('signCookie') or 
                        working_stream.get('signCookie') or
                        s["client"].session.cookies.get("signCookie") or 
                        global_cookie or ""
                    )
                    
                    if not working_cookie and "hakunaymatata" in working_stream.get("url", ""):
                        logger.warning("PROTECTED CDN DETECTED WITH NO COOKIE AFTER PING! Attempting session flush...")
                        working_cookie = s["client"].session.cookies.get("signCookie") or ""

                    subtitles_source = p_data.get("subTitleList", []) or subtitles_source
                    logger.info(f"RESOURCE MIRROR SUCCESS: Recovered stream via User-Resource {r_id} (Cookie: {'YES' if working_cookie else 'NO'})")
                    break
        except Exception as e:
            logger.warning(f"Resource Mirror Lookup Failed: {e}")

    is_hevc = any("h265" in st.get("url", "").lower() or "x265" in st.get("url", "").lower() or "hev1" in st.get("url", "").lower() or "h.265" in st.get("url", "").lower() for st in streams)
    h265_candidate = next((st for st in streams if any(bad in st.get("url", "").lower() for bad in ["h265", "x265", "hev1", "h.265"])), None)
    
    # ⚠️ MODIFIÉ : FORCE best H.264 (trié par qualité desc)
    h264_candidates = [st for st in streams if "h264" in st.get("url", "").lower()]
    if h264_candidates:
        def _qnum_sort(st):
            q = str(st.get("quality", "0")).replace("p", "").replace("P", "").strip()
            try: return int(q) if q.isdigit() else 0
            except: return 0
        h264_candidates.sort(key=_qnum_sort, reverse=True)
        working_stream = h264_candidates[0]
        working_cookie = working_stream.get("signCookie") or global_cookie or ""
        logger.info(f"FORCING best H.264 ({working_stream.get('quality')}): {working_stream.get('url', '')[:60]}")
    elif is_hevc and h265_candidate:
        logger.info(f"Detected HEVC/H.265 (no H.264 available). Serving H.265...")
        working_stream = h265_candidate
        working_cookie = working_stream.get("signCookie") or global_cookie or ""
    # ⚠️ FIN MODIFIÉ

    if not working_stream:
        logger.info(f"Entering Phase 4 Cluster Rotation for {subject_id}")
        clusters = [
            ("https://apig.inmoviebox.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://api5.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://api-sin.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://v-ios.aoneroom.com", "/wefeed-mobile-bff/subject-api/play-info"),
            ("https://h5-api.aoneroom.com", "/wefeed-h5api-bff/subject/detail-rec"),
            ("https://h5.aoneroom.com", "/index/video/v_detail")
        ]
        
        hdrs = {"User-Agent": "MovieBox/4.0.02 (Android 14; Pixel 6)", "X-M-Version": "4.0.02"}
        orig_base = s["client"].BASE_URL
        for cluster_url, endpoint in clusters:
            try:
                logger.info(f"ROTATING CLUSTER: {cluster_url} -> {endpoint}")
                s["client"].BASE_URL = cluster_url
                
                pms = {'subjectId': subject_id, 'se': season, 'ep': episode, 'quality': official_quality}
                if "detail-rec" in endpoint:
                    pms["perPage"] = 12
                    pms["page"] = 1
                
                method = 'POST' if "v_detail" in endpoint else 'GET'
                
                fb_res = s["client"].request(method, endpoint, params=pms if method=='GET' else None, data=pms if method=='POST' else None, headers=hdrs)
                if not isinstance(fb_res, dict):
                    logger.warning(f"Cluster {cluster_url} returned invalid response type: {type(fb_res)}")
                    continue
                fb_data = fb_res.get('data', {})
                srcs = fb_data.get('streamList') or fb_data.get('streams') or []
                
                logger.info(f"Cluster {cluster_url} Result: {len(srcs)} streams found")
                if not srcs and fb_data.get('url'): srcs = [fb_data]
                
                for cand in srcs:
                    url = cand.get('url')
                    if not url: continue
                    cookie = cand.get('signCookie') or fb_res.get('signCookie') or fb_data.get('signCookie') or s["client"].session.cookies.get("signCookie") or global_cookie or ""
                    try:
                        v_res = requests.head(url, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}, timeout=3, verify=False)
                        if v_res.status_code in [200, 206]:
                            working_stream = cand
                            working_cookie = cookie
                            subtitles_source = fb_data.get("subTitleList", []) or subtitles_source
                            logger.info(f"CLUSTER SUCCESS: Recovered stream via {cluster_url} Cluster")
                            break
                    except: continue
                if working_stream: break
            except Exception as e:
                logger.warning(f"Cluster {cluster_url} failed: {e}")
            finally:
                s["client"].BASE_URL = orig_base
    
    if not working_stream and streams:
        working_stream = streams[0]
        working_cookie = working_stream.get("signCookie") or global_cookie or ""

    if not working_stream:
        logger.error(f"RESOLUTION FAILURE: No usable streams found for subject {subject_id}")
        raise HTTPException(status_code=404, detail="No streams found.")

    total_duration = 0
    source_method = "metadata"
    try:
        subject_detail = s["content"].get_movie_detail(subject_id).get("data") or {}
        runtime_str = subject_detail.get("runtime") or subject_detail.get("duration") or subject_detail.get("totalDuration") or "0"
        for sl in subject_detail.get("seasonList", []):
            if str(sl.get("season")) == str(season):
                for ep_item in sl.get("episodeList", []):
                    if str(ep_item.get("episode")) == str(episode):
                        runtime_str = ep_item.get("runtime") or ep_item.get("duration") or ep_item.get("totalDuration") or runtime_str
                        break
        
        runtime_str = str(runtime_str)
        if ":" in runtime_str:
            parts = [int(p) for p in runtime_str.split(':')]
            if len(parts) == 3: total_duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2: total_duration = parts[0] * 60 + parts[1]
        else:
            total_duration = int(''.join(filter(str.isdigit, runtime_str))) * 60
    except Exception as e:
        logger.error(f"Metadata duration extraction failed: {e}")
    
    if (total_duration < 600 or total_duration > 15000) and working_stream.get("url", "").lower().endswith(".m3u8"):
        try:
           target_v = working_stream.get("url")
           logger.info(f"Metadata suspicious. Engaging HLS Parse for {target_v[:60]}...")
           h_res = httpx.get(target_v, headers={"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": working_cookie}, verify=False, timeout=5)
           if h_res.status_code == 200:
              lines = h_res.text.splitlines()
              h_dur = sum(float(line.split(":")[1].split(",")[0]) for line in lines if line.startswith("#EXTINF:"))
              if h_dur > 0:
                 total_duration = int(h_dur)
                 source_method = "hls-parse"
        except Exception as he:
           logger.warning(f"HLS duration parse failed: {he}")

    if source_method == "metadata" and (total_duration < 600 or total_duration > 15000):
        try:
           target_v = working_stream.get("url")
           if target_v:
              logger.info(f"Metadata suspicious ({total_duration}s). Engaging FFPROBE for {target_v[:60]}...")
              ff_cmd = [
                  'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                  '-of', 'default=noprint_wrappers=1:nokey=1',
                  '-headers', f'Cookie: {working_cookie}\r\nUser-Agent: ExoPlayerLib/2.18.7\r\n',
                  target_v
              ]
              ff_proc = subprocess.run(ff_cmd, capture_output=True, text=True, timeout=5)
              if ff_proc.returncode == 0 and ff_proc.stdout.strip():
                  f_dur = float(ff_proc.stdout.strip())
                  if f_dur > 0:
                      total_duration = int(f_dur)
                      source_method = "ffprobe"
        except Exception as fe:
           logger.warning(f"FFPROBE duration probe failed: {fe}")

    if total_duration < 60: 
        total_duration = 3600 

    logger.info(f"DURATION LOCK [{source_method}]: {subject_id} S{season}E{episode} -> {total_duration}s (Source: {runtime_str})")

    all_subtitles = []
    seen_sub_urls = set()

    def add_subtitle_track(sub: dict):
        if not isinstance(sub, dict): return
        url = sub.get("url") or ""
        if not url or "aa348f2541d13ffe" in url:
            return 
        
        try:
            sz = int(sub.get("size") or 0)
            if 0 < sz <= 50: return
        except: pass

        if url not in seen_sub_urls:
            seen_sub_urls.add(url)
            lan_name = sub.get("lanName") or sub.get("lan") or "Unknown"
            sub["lanName"] = lan_name
            all_subtitles.append(sub)

    for st_sub in subtitles_source:
        add_subtitle_track(st_sub)

    try:
        ext_hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        sub_resource_id = str(working_stream.get("id") or "")
        
        if not sub_resource_id or sub_resource_id == "None":
            try:
                sd_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=ext_hdrs)
                sd_detectors = sd_res.get('data', {}).get('resourceDetectors', [])
                if sd_detectors:
                    sub_resource_id = str(sd_detectors[0].get('resourceId') or subject_id)
            except:
                sub_resource_id = subject_id
        
        ext_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get-ext-captions', 
                                      params={'resourceId': sub_resource_id, 'subjectId': subject_id, 'episode': episode}, 
                                      headers=ext_hdrs)
        ext_list = ext_res.get("data", {}).get("extCaptions") or ext_res.get("data", {}).get("list") or []
        for esub in ext_list:
            add_subtitle_track(esub)
    except Exception as se:
        logger.warning(f"External subtitle probe failed: {se}")

    best_sub = next((s.get("url") for s in all_subtitles if s.get("lan") == "en" or "english" in (s.get("lanName") or "").lower()), None)
    if not best_sub and all_subtitles:
        best_sub = all_subtitles[0].get("url")

    is_dash_or_hls = (
        working_stream.get("url", "").lower().endswith(".mpd")
        or ".mpd" in working_stream.get("url", "").lower()
        or working_stream.get("url", "").lower().endswith(".m3u8")
        or ".m3u8" in working_stream.get("url", "").lower()
    )
    stream_referer = "https://sportslive.wine" if is_dash_or_hls else ""

    headers_dict = {
        "User-Agent": "ExoPlayerLib/2.19.1",
        "Cookie": working_cookie or ""
    }
    if stream_referer:
        headers_dict["Referer"] = stream_referer

    return {
        "url": working_stream.get("url", ""),
        "cookie": working_cookie,
        "duration": total_duration,
        "subtitles": all_subtitles,
        "subtitle_url": best_sub,
        "isHls": working_stream.get("url", "").lower().endswith(".m3u8") or ".m3u8" in working_stream.get("url", "").lower(), 
        "streamId": working_stream.get("id"),
        "qualityList": sorted(
            list(set([st.get("quality") for st in streams if st.get("quality")])),
            key=lambda q: int(str(q).replace("p", "")) if str(q).replace("p", "").isdigit() else 0,
            reverse=True
        ),
        "episode": episode,
        "season": season,
        "referer": stream_referer,
        "headers": headers_dict
    }

# ⚠️ NOUVEAU : ENDPOINT /streams qui renvoie TOUTES les qualités disponibles
@app.get("/streams/{subject_id}")
def get_all_streams(
    subject_id: str,
    season: int = 1,
    episode: int = 1,
    session_id: Optional[str] = Cookie(None)
):
    """
    Retourne TOUTES les qualités disponibles (360p, 480p, 720p, 1080p, ...)
    Chaque qualité a son URL de proxy dédiée.
    """
    s = get_session(session_id)
    try:
        subject_detail = s["content"].get_movie_detail(subject_id).get("data") or {}
        is_movie = (str(subject_detail.get("subjectType") or subject_detail.get("type")) == "1")
    except:
        is_movie = False

    logger.info(f"LIST ALL STREAMS: ID={subject_id} Type={'Movie' if is_movie else 'Series'}")
    res_se = None if is_movie else season
    res_ep = None if is_movie else episode

    def is_deprecation_notice_url(u: str) -> bool:
        if not u: return False
        lower = u.lower()
        return (
            "notice.mp4" in lower
            or ("macdn.aoneroom.com" in lower and "/other/" in lower)
            or "1c7de0bd3393702d9191801f15f88f8d" in lower
            or "9a0461bc39da389663bf3dbb17091d3f" in lower
            or "80da52722ee7de7a4303290af6783424" in lower
            or "b164fbfb4347792950bdfbfb563d39d9" in lower
        )

    # 1. Play-info
    res = s["stream"].get_play_info_v2(subject_id, season=res_se, episode=res_ep)
    data = res.get("data", {}) if isinstance(res, dict) else {}
    streams = data.get("streams") or data.get("streamList") or []

    if not streams:
        res = s["stream"].get_play_info(subject_id, season=res_se, episode=res_ep)
        data = res.get("data", {}) if isinstance(res, dict) else {}
        streams = data.get("streamList") or data.get("streams") or []

    # 2. Resource mirrors
    try:
        r2_se = 0 if is_movie else season
        r2_ep = 0 if is_movie else episode
        res2 = s["stream"].get_resources_v2(subject_id, season=r2_se, episode=r2_ep)
        r2_items = res2.get("data", {}).get("list") or []
        for r_item in r2_items:
            r_link = r_item.get("resourceLink") or r_item.get("downloadUrl") or r_item.get("url")
            if r_link:
                streams.append({
                    "url": r_link,
                    "quality": f"{r_item.get('resolution')}p" if r_item.get('resolution') else "Auto",
                    "signCookie": r_item.get("signCookie") or "",
                    "codec": r_item.get("codecName") or "",
                    "id": str(r_item.get("resourceId") or ""),
                })
    except Exception as e:
        logger.warning(f"Mirror fetch failed: {e}")

    # 3. Anti-piracy recovery
    recovered = []
    for _st in streams:
        st_url = _st.get("url", "")
        st_rid = str(_st.get("id") or "")
        if is_deprecation_notice_url(st_url) and st_rid:
            try:
                rec_res = s["stream"].recover_resource_position_v2(subject_id, st_rid, st_url)
                rec_list = rec_res.get("data", {}).get("list") or []
                if rec_list:
                    rec_item = rec_list[0]
                    real_link = rec_item.get("resourceLink") or rec_item.get("url")
                    if real_link and not is_deprecation_notice_url(real_link):
                        recovered.append({
                            "url": real_link,
                            "quality": f"{rec_item.get('resolution')}p" if rec_item.get('resolution') else _st.get("quality", "Auto"),
                            "signCookie": rec_item.get("signCookie") or _st.get("signCookie") or "",
                            "codec": rec_item.get("codecName") or _st.get("codec", ""),
                            "id": str(rec_item.get("resourceId") or st_rid),
                        })
            except Exception as e:
                logger.warning(f"Recovery failed: {e}")
        elif not is_deprecation_notice_url(st_url):
            recovered.append(_st)

    if recovered:
        streams = recovered

    # 4. Group by quality — pick BEST codec per quality (H.264 > others > H.265)
    by_quality = {}
    for st in streams:
        q_raw = str(st.get("quality", "")).replace("p", "").replace("P", "").strip()
        try:
            qn = int(q_raw) if q_raw.isdigit() else 0
        except:
            qn = 0
        if qn == 0:
            continue

        url_l = st.get("url", "").lower()
        is_h264 = "h264" in url_l
        is_h265 = "h265" in url_l or "x265" in url_l or "hev1" in url_l
        priority = 0 if is_h264 else (2 if is_h265 else 1)

        if qn not in by_quality or priority < by_quality[qn]["priority"]:
            codec = "h264" if is_h264 else ("h265" if is_h265 else "auto")
            by_quality[qn] = {
                "quality": f"{qn}p",
                "qualityNum": qn,
                "url": st.get("url"),
                "proxyUrl": f"/stream-proxy/{subject_id}?season={season}&episode={episode}&quality={qn}p",
                "codec": codec,
                "priority": priority,
                "streamId": st.get("id"),
            }

    result = sorted(by_quality.values(), key=lambda x: x["qualityNum"], reverse=True)

    # 5. Duration
    duration = 0
    runtime_str = subject_detail.get("runtime") or subject_detail.get("duration") or "0"
    try:
        runtime_str = str(runtime_str)
        if ":" in runtime_str:
            parts = [int(p) for p in runtime_str.split(':')]
            if len(parts) == 3: duration = parts[0] * 3600 + parts[1] * 60 + parts[2]
            elif len(parts) == 2: duration = parts[0] * 60 + parts[1]
        else:
            duration = int(''.join(filter(str.isdigit, runtime_str))) * 60
    except:
        pass

    logger.info(f"STREAMS FOUND: {[s['quality'] for s in result]}")

    return {
        "streams": result,
        "qualities": [s["quality"] for s in result],
        "duration": duration,
        "isMovie": is_movie,
        "season": season,
        "episode": episode,
        "subtitles": data.get("subTitleList", []),
    }
# ⚠️ FIN NOUVEAU

# ⚠️ MODIFIÉ : stream_proxy accepte maintenant quality
@app.get("/stream-proxy/{subject_id}")
async def stream_proxy(
    subject_id: str,
    season: int = 1,
    episode: int = 1,
    quality: str = "1080p",
    request: Request = None,
    session_id: Optional[str] = Cookie(None)
):
    """
    Proxy vidéo avec support Range + proxy externe.
    Résout : SSL invalide du CDN + geo-restriction éventuelle.
    """
    try:
        stream_info = get_stream(subject_id, season, episode, quality=quality, session_id=session_id)
        url = stream_info.get("url")
        cookie = stream_info.get("cookie")
        
        if not url:
            raise HTTPException(status_code=404, detail="Aucun stream trouvé")
        
        logger.info(f"PROXY (quality={quality}): {url[:80]}...")
        
        headers = {"User-Agent": "ExoPlayerLib/2.19.1"}
        if cookie:
            headers["Cookie"] = cookie
        
        if request and request.headers.get("range"):
            headers["Range"] = request.headers.get("range")
            logger.info(f"PROXY: Range={request.headers.get('range')}")
        
        proxy_url = os.getenv("EXTERNAL_PROXY")
        
        client = httpx.AsyncClient(
            verify=False,
            proxy=proxy_url if proxy_url else None,
            timeout=60.0,
            follow_redirects=True
        )
        
        req = client.build_request("GET", url, headers=headers)
        r = await client.send(req, stream=True)
        
        logger.info(f"PROXY: CDN responded {r.status_code}")
        
        if r.status_code not in (200, 206):
            await client.aclose()
            raise HTTPException(status_code=r.status_code, detail=f"CDN error: {r.status_code}")
        
        response_headers = {
            "Content-Type": "video/mp4",
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
        if r.headers.get("content-length"):
            response_headers["Content-Length"] = r.headers["content-length"]
        if r.headers.get("content-range"):
            response_headers["Content-Range"] = r.headers["content-range"]
        
        async def stream_chunks():
            try:
                async for chunk in r.aiter_raw(chunk_size=65536):
                    yield chunk
            except Exception as e:
                logger.warning(f"Stream interrupted: {e}")
            finally:
                await client.aclose()
        
        return StreamingResponse(
            stream_chunks(),
            status_code=r.status_code,
            headers=response_headers
        )
    except Exception as e:
        logger.error(f"PROXY ERROR: {e}")
        raise HTTPException(status_code=502, detail=f"Proxy error: {str(e)}")
# ⚠️ FIN MODIFIÉ

import os

@app.get("/download/{subject_id}")
async def proxy_download(
    request: Request,
    subject_id: str, 
    season: int = 1, 
    episode: int = 1, 
    quality: str = "720p", 
    title: str = "Movie",
    session_id: Optional[str] = Cookie(None)
):
    s = get_session(session_id)
    
    logger.info(f"Targeting Carrier 301 Raw Mirrors for {subject_id}...")
    c_res = s["client"].request('POST', '/index/video/v_detail', data={'subjectId': subject_id, 'carrier': '301', 'quality': quality})
    data = c_res.get("data")
    if not isinstance(data, dict): data = {}
    streams = data.get("streamList") or data.get("streams") or []
    
    if not streams:
        res = s["stream"].get_play_info(subject_id, season=season, episode=episode)
        data = res.get("data", {})
        streams = data.get("streamList") or data.get("streams") or []
    
    if not streams: 
        raise HTTPException(status_code=404, detail="No downloadable mirrors found")
    
    match = None
    for st in streams:
        if ".mp4" in st.get("url", "").lower():
            match = st
            break
    if not match: match = streams[0]
            
    url = match.get("url")
    cookie = data.get("signCookie") or match.get("signCookie") or ""
    if not url: raise HTTPException(status_code=404)
    
    clean_title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
    
    if ".mp4" in url.lower():
        import httpx
        req_hdrs = {"User-Agent": "ExoPlayerLib/2.18.7", "Cookie": cookie}
        range_header = request.headers.get("Range")
        if range_header: req_hdrs["Range"] = range_header
            
        client = httpx.AsyncClient(verify=False)
        req = client.build_request("GET", url, headers=req_hdrs)
        r = await client.send(req, stream=True)
        
        filename = f"{clean_title}_S{season}_E{episode}.mp4"
        headers = {
            "Content-Disposition": f"attachment; filename=\"{filename}\"",
            "Accept-Ranges": r.headers.get("Accept-Ranges", "bytes"),
            "Content-Length": r.headers.get("Content-Length", ""),
            "Content-Range": r.headers.get("Content-Range", ""),
            "Content-Type": "video/mp4"
        }
        
        async def stream_generator():
            async for chunk in r.aiter_bytes(chunk_size=1024 * 1024): yield chunk

        return StreamingResponse(
            stream_generator(),
            status_code=r.status_code,
            headers={k: v for k, v in headers.items() if v},
            background=httpx.AsyncClient().aclose
        )
        
    import subprocess
    filename = f"{clean_title}_S{season}_E{episode}.ts"
    
    def iter_ffmpeg():
        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-headers', f'Cookie: {cookie}\r\nUser-Agent: ExoPlayerLib/2.18.7\r\n',
            '-i', url,
            '-c', 'copy', '-f', 'mpegts', '-'
        ]
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            while True:
                chunk = process.stdout.read(2048 * 1024)
                if not chunk: break
                yield chunk
        finally:
            process.terminate()

    return StreamingResponse(
        iter_ffmpeg(), 
        media_type="video/mp2t",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\""
        }
    )

@app.get("/subtitles/{subject_id}")
def get_subtitles(subject_id: str, se: int = 1, ep: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        resource_id = subject_id
        try:
            det_res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get', params={'subjectId': subject_id}, headers=hdrs)
            detectors = det_res.get('data', {}).get('resourceDetectors', [])
            if detectors:
                resource_id = detectors[0].get('resourceId') or subject_id
        except: pass

        res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/get-ext-captions', 
                                    params={'resourceId': resource_id, 'subjectId': subject_id, 'episode': ep}, 
                                    headers=hdrs)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        ls = data.get("extCaptions") or data.get("list") or []
        return {"code": 0, "data": {"list": ls}}
    except:
        return {"code": 0, "data": {"list": []}}

@app.get("/history/position")
def get_history_position(subject_id: str, resource_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/resource-position', params={'subjectId': subject_id, 'resourceId': resource_id})
    return res

@app.post("/history/position")
def save_history_position(subject_id: str, resource_id: str, position: int, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('POST', '/wefeed-mobile-bff/subject-api/resource-position', data={'subjectId': subject_id, 'resourceId': resource_id, 'position': position})
    return res

@app.post("/history/seen")
def mark_have_seen(subject_id: str = None, progress: int = 0, total: int = 0, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if not subject_id: return {"code": -1, "msg": "Missing ID"}
    res = s["user"].report_history(subject_id, progress, total)
    return res

@app.post("/analytics/operation")
def track_operation(action: str, target: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    res = s["client"].request('POST', '/wefeed-mobile-bff/statistics/user-operation', data={'action': action, 'target': target})
    return res

@app.get("/subtitles/search")
def subtitle_search(query: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        hdrs = {"User-Agent": "Dalvik/2.1.0 (Linux; U; Android 13; SM-S918B Build/TP1A.220624.014)", "X-M-Version": "11.7.0"}
        res = s["client"].request('GET', '/wefeed-mobile-bff/subject-api/subtitle-search', params={'q': query}, headers=hdrs)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        ls = data.get("items") or data.get("list") or []
        return {"code": 0, "data": {"list": ls}}
    except: return {"code": 0, "data": {"list": []}}


@app.get("/history")
def get_history(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    h = load_local_history()
    
    default_history = h.get("default", [])
    user_specific = h.get(session_id, []) if session_id else []
    blacklist = set(h.get("blacklist", []))
    
    combined_history_dict = {str(x.get("subjectId") or x.get("id")): x for x in default_history + user_specific}
    user_history = list(combined_history_dict.values())
    
    if not s["auth"].is_guest_mode:
        try:
            res = s["user"].get_history(page=page)
            data = res.get("data", {})
            if not isinstance(data, dict): data = {}
            cloud_list = data.get("items") or data.get("list") or []
            
            seen_ids = set(combined_history_dict.keys())
            for c in cloud_list:
                sid_str = str(c.get("subjectId"))
                if sid_str not in seen_ids and sid_str not in blacklist:
                    mapped = map_item(c)
                    mapped["seeTime"] = c.get("seeTime") or c.get("updateTime") or c.get("progress") or 0
                    mapped["subjectId"] = c.get("subjectId")
                    mapped["id"] = c.get("subjectId")
                    user_history.append(mapped)
        except Exception as e:
            pass
        
    user_history = [x for x in user_history if x.get("subjectId") and str(x.get("subjectId")) != "None"]
    user_history = [x for x in user_history if str(x.get("subjectId")) not in blacklist]
    user_history.sort(key=lambda x: int(x.get("seeTime", 0) or 0), reverse=True)
    
    return {"code": 0, "data": {"list": user_history}}


@app.get("/watchlist")
def get_watchlist(page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        return {"code": 0, "data": {"list": []}}
    try:
        res = s["user"].get_watchlist(page=page)
        data = res.get("data", {})
        if not isinstance(data, dict): data = {}
        cloud_list = data.get("items") or data.get("list") or []
        return {"code": 0, "data": {"list": [map_item(x) for x in cloud_list]}}
    except:
        return {"code": 0, "data": {"list": []}}

@app.post("/history/delete/{subject_id}")
def delete_history_item(subject_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        raise HTTPException(status_code=401, detail="Please Sign In to manage your history.")
    res = s["user"].report_history(subject_id, 0, 0, status=0) 
    return {"status": "success", "raw": res}


@app.post("/watchlist/toggle")
def toggle_watchlist(subject_id: str, active: bool, subject_type: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        raise HTTPException(status_code=401, detail="Please Sign In to manage your watchlist.")
    action = 1 if active else 2
    res = s["user"].toggle_watchlist(subject_id, action=action, subject_type=subject_type)
    return {"status": "success", "raw": res}

class ProgressReport(BaseModel):
    subject_id: str
    progress_ms: int
    total_ms: int
    status: int = 1

@app.post("/history/progress")
def report_progress(req: ProgressReport, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if s["auth"].is_guest_mode:
        return {"status": "success"}
    return s["user"].report_history(req.subject_id, req.progress_ms, req.total_ms, req.status)

@app.post("/launch-player")
def launch_player(player: str, url: str, cookie: Optional[str] = None, subject_id: Optional[str] = None, resource_id: Optional[str] = None, season: Optional[int] = None, episode: Optional[int] = None, title: Optional[str] = None, cover: Optional[str] = None, start_time: int = 0, subtitle_url: Optional[str] = None, duration: int = 0, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    if subject_id:
        if not s["auth"].is_guest_mode:
            try:
                s["client"].request('POST', '/wefeed-mobile-bff/subject-api/have-seen', data={'subjectId': subject_id})
            except: pass
            try:
                s["user"].report_history(subject_id, int(start_time) * 1000, 6000000, status=1)
            except Exception as e:
                print("History sync failed:", e)

    import subprocess
    
    if (not url or "bridge" in url or "compat" in url or "proxy" in url) and subject_id:
        try:
            logger.info(f"Resolving RAW stream for native launch: {subject_id} S{season}E{episode} (Resource: {resource_id})")
            stream_info = get_stream(subject_id, season=season or 1, episode=episode or 1, resource_id=resource_id, session_id=session_id)
            url = stream_info.get("url")
            cookie = stream_info.get("cookie")
            duration = stream_info.get("duration", duration)
            if not subtitle_url:
                subtitle_url = stream_info.get("subtitle_url")
            if url: logger.info(f"SUCCESS: Resolved {url[:60]}...")
        except Exception as e:
            logger.error(f"Native Resolution failed: {e}")

    if not url:
        return {"status": "error", "message": "Empty URL"}

    if cookie:
        cookie = cookie.strip().rstrip(';')
        if "signCookie=" not in cookie:
            if "CloudFront-Policy" in cookie:
                pass 
            else:
                cookie = f"signCookie={cookie}"
    else:
        cookie = s["client"].session.cookies.get("signCookie") or ""
        if not cookie and ("hakunaymatata" in url or "hindi" in (title or "").lower()):
            cookie = s["auth"].token or ""
            if not cookie:
                import time
                cookie = f"guest_{int(time.time())}_{subject_id}"
                logger.info("Using Generated Guest Signature")
        
        if cookie and "signCookie=" not in cookie:
            cookie = f"signCookie={cookie}"
    
    if "sign=" in url:
        logger.info("SIGN PARAMETER DETECTED IN URL: Suppressing external cookie injection.")
        cookie = None

    ua = "ExoPlayerLib/2.19.1"
    referer = None
    if ".mpd" in url.lower() or ".m3u8" in url.lower():
        referer = "https://sportslive.wine"
    
    if player.lower() == "mpv":
        cmd = ["mpv", f"--user-agent={ua}", "--cache=yes"]
        if referer:
            cmd.append(f"--referrer={referer}")
        if "hakunaymatata.com" in url or ".mp4" in url.lower():
            cmd.append("--ytdl=no")
        if title: cmd.append(f'--force-media-title={title}')
        if duration > 0: cmd.append(f"--length={duration}")
        if cookie:
            cmd.append(f"--http-header-fields=Cookie: {cookie}")
            if ".mpd" in url:
                cmd.append(f"--demuxer-lavf-o=headers=Cookie: {cookie}")
        if start_time > 0: cmd.append(f"--start={start_time}")
        if subtitle_url: cmd.append(f'--sub-file={subtitle_url}')
        cmd.append(url)
    else:
        cmd = ["vlc", f"--http-user-agent={ua}", "--network-caching=5000"]
        if referer:
            cmd.append(f"--http-referrer={referer}")
        if cookie: cmd.append(f':http-header-fields=Cookie: {cookie}')
        if start_time > 0: cmd.append(f"--start-time={start_time}")
        if subtitle_url: cmd.append(f'--sub-file={subtitle_url}')
        cmd.append(url)
    
    def win_quote(s):
        if ' ' in s or '&' in s or '?' in s or '=' in s:
            return f'"{s}"'
        return s
    
    cmd_str = ' '.join(map(win_quote, cmd))
    logger.info(f"EXEC CMD (WIN): {cmd_str}")
    
    subprocess.Popen(cmd_str, shell=True, creationflags=0x00000200)
    return {"status": "success", "url": url}

@app.get("/post/count/{subject_id}")
def get_post_count(subject_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("GET", "/wefeed-mobile-bff/post/count/subject", params={"subjectId": subject_id})
        count = res.get("data", {}).get("count") or "0"
        return {"code": 0, "count": count}
    except:
        return {"code": 0, "count": "0"}

@app.get("/groups/trending")
def get_trending_groups(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/group/list/trending-entrance", data={})
        data = res.get("data", {})
        items = data.get("items") or []
        return {"code": 0, "data": items}
    except:
        return {"code": 0, "data": []}

@app.post("/post/like")
def like_post(post_id: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/interactive/post/like", data={"postId": post_id})
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.post("/post/create")
def create_post(subject_id: str, content: str, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        data = {
            "subjectId": subject_id,
            "content": content,
            "type": "1"
        }
        res = s["client"].request("POST", "/wefeed-mobile-bff/post/create", data=data)
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.get("/groups/interactive")
def get_interactive_posts(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/interactive/post/list", 
                                 data={"page": 1, "pageSize": 20})
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.get("/post/list/{subject_id}")
def get_subject_posts(subject_id: str, page: int = 1, session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    try:
        res = s["client"].request("POST", "/wefeed-mobile-bff/post/list/subject", 
                                 data={"subjectId": subject_id, "page": page, "pageSize": 10})
        return res
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.get("/debug_history")
def debug_history(session_id: Optional[str] = Cookie(None)):
    s = get_session(session_id)
    results = {}
    for i in range(1, 6):
        try:
            res = s["client"].request(
                "GET",
                "/wefeed-mobile-bff/subject-api/see-list-v2",
                params={"page": "1", "pageSize": "10", "seeType": str(i)}
            )
            data = res.get("data", {})
            if not isinstance(data, dict): data = {}
            lst = data.get("list") or data.get("items") or res.get("list") or []
            results[f"seeType_{i}_count"] = len(lst)
            if len(lst) > 0:
                results[f"seeType_{i}_sample_keys"] = list(lst[0].keys())
        except Exception as e:
            results[f"seeType_{i}_error"] = str(e)
            
    try:
        results["tab_record"] = s["client"].request("GET", "/wefeed-mobile-bff/tab/play-record")
    except: pass
    return results

@app.get("/sub-proxy")
async def subtitle_proxy(u: str):
    async with httpx.AsyncClient(verify=False) as client:
        res = await client.get(u, headers={"User-Agent": "ExoPlayerLib/2.18.7"}, follow_redirects=True)
        return Response(content=res.content, media_type="text/vtt", headers={"Access-Control-Allow-Origin": "*"})

@app.post("/launch-player")
def launch_player(
    player: str = Query(...),
    url: str = Query(...),
    cookie: Optional[str] = Query(None),
    subject_id: Optional[str] = Query(None),
    season: Optional[int] = Query(None),
    episode: Optional[int] = Query(None),
    title: Optional[str] = Query(None),
    cover: Optional[str] = Query(None),
    start_time: Optional[int] = Query(0),
    subtitle_url: Optional[str] = Query(None),
    duration: Optional[int] = Query(0)
):
    logger.info(f"Launching external player: {player} for url: {url[:100]}... (start_time: {start_time})")
    cmd = []
    if player.lower() == "mpv":
        cmd = ["mpv", url]
        if title:
            display_title = title
            if season and episode:
                display_title += f" S{season}E{episode}"
            cmd.append(f"--title={display_title}")
        if start_time and start_time > 0:
            cmd.append(f"--start={start_time}")
        if subtitle_url:
            cmd.append(f"--sub-file={subtitle_url}")
        cmd.append("--user-agent=ExoPlayerLib/2.18.7")
        if cookie:
            cmd.append(f"--http-header-fields=Cookie: {cookie}")
    elif player.lower() == "vlc":
        cmd = ["vlc", url]
        if title:
            display_title = title
            if season and episode:
                display_title += f" S{season}E{episode}"
            cmd.extend(["--meta-title", display_title])
        if start_time and start_time > 0:
            cmd.extend(["--start-time", str(start_time)])
        if subtitle_url:
            cmd.extend(["--sub-file", subtitle_url])
        cmd.extend(["--http-user-agent", "ExoPlayerLib/2.18.7"])
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported player: {player}")
        
    try:
        creation_flags = 0
        if os.name == 'nt':
            creation_flags = 0x00000008 | 0x00000200
        logger.info(f"Executing: {' '.join(cmd)}")
        subprocess.Popen(cmd, creationflags=creation_flags, close_fds=True)
        return {"status": "success", "message": f"Launched {player}"}
    except Exception as e:
        logger.error(f"Failed to launch player: {e}")
        try:
            subprocess.Popen(cmd, shell=True)
            return {"status": "success", "message": f"Launched {player} (fallback shell)"}
        except Exception as e2:
            logger.error(f"Fallback launch failed: {e2}")
            raise HTTPException(status_code=500, detail=f"Could not launch {player}: {str(e2)}")

if __name__ == "__main__":
    uvicorn.run("moviebox_api_server:app", host="0.0.0.0", port=8000, reload=False)
