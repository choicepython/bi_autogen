
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate, br",
}


class PortalToken:

    def __init__(self) -> None:
        self.iam_token_url = {
            "beta": "http://zhiwen.make-beta.huawei.com/makegpt/llm/general/iam_token3",
            "prod": "https://w3.huawei.com/ikbg/llm/general/iam_token3",
        }
        self.soa_token_url = {
            "beta": "https://zhiwen.make-beta.huawei.com/makegpt/llm/general/dynamic_token",
            "prod": "https://w3.huawei.com/ikbg/llm/general/dynamic_token",
        }

    async def get_portal_token(self, env: str = "beta", use: str = "soa") -> str:
        url_map = self.soa_token_url if use == "soa" else self.iam_token_url
        url = url_map.get(env)
        if not url:
            url = url_map.get("prod")
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                resp = await client.get(url, ssl=False, headers=DEFAULT_HEADERS)
                if resp.status != 200:
                    logger.error("[get_portal_token] error: %s", resp.status)
                    return ""
                result = await resp.json()
        except Exception as e:
            logger.error("[get_portal_token] error: %s", e)
            return ""
        logger.info("[get_portal_token]-%s:%s success", use, env)
        return result["data"]

    @classmethod
    def get_roma_header(cls, env: str = "beta") -> dict[str, str]:
        header: dict[str, str] = {
            "X-HW-ID": "com.huawei.make.mes.mesai.ikbg",
        }
        if env == "beta":
            header["X-HW-APPKEY"] = "FuIdlmhcsr4MuQHR+xIhDQ=="
        else:
            header["X-HW-APPKEY"] = "Q3txolbj7z9cP1Qe2THP2Q=="
        return header


async def build_headers(use: str | None = None, env: str = "beta", headers: dict | None = None) -> dict[str, Any]:
    allowed_use = ["soa", "roma", "ikbg", "mqs", "jwt"]
    allowed_env = ["beta", "prod", "sit", "uat", "local"]
    header: dict[str, Any] = {}
    portal_token = PortalToken()

    if (use is None) or (use not in allowed_use) or (env not in allowed_env):
        return header
    if headers is None:
        headers = {}
    # 机机接口
    if use.lower() in ["soa", "jwt"]:
        data = await portal_token.get_portal_token(env=env, use=use)
        headers.update({"Authorization": data})
    elif use == "roma":
        data = portal_token.get_roma_header(env=env)
        header.update(data)
    return header