from uuid import uuid4
from curl_cffi import requests
import random, time, json, re
from geeked.sign import Signer
from anti_bot.pow_budget import check_cancelled


class CaptchaSolveRejected(Exception):
    """GeeTest accepted the verify request but rejected its solution."""

    def __init__(self, response: dict):
        self.response = response
        super().__init__(f"Failed to submit captcha: {response}")


class Geeked:
    def __init__(
        self,
        captcha_id: str,
        lang: str = "rus",
        *,
        session=None,
        request_headers=None,
        exchange_logger=None,
        cancel_event=None,
        **kwargs,
    ):
        self.pass_token = None
        self.lot_number = None
        self.captcha_id = captcha_id
        self.challenge = str(uuid4())
        self.lang = lang
        self.cancel_event = cancel_event
        self.callback = Geeked.random()
        self.session = session if session is not None else requests.Session(
            impersonate="chrome124",
            **kwargs,
        )
        self.owns_session = session is None
        self.base_url = "https://gcaptcha4.geetest.com"
        self.exchange_logger = exchange_logger
        self.request_headers = request_headers or {
            "connection": "keep-alive",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "sec-ch-ua-mobile": "?0",
            "accept": "*/*",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-dest": "script",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-US,en;q=0.9"
        }
        if self.owns_session:
            self.session.headers = dict(self.request_headers)

    @staticmethod
    def random() -> str:
        return f"geetest_{int(random.random() * 10000) + int(time.time() * 1000)}"

    def format_response(self, response: str) -> dict:
        match = re.fullmatch(rf"\s*{re.escape(self.callback)}\((.*)\)\s*;?\s*", response, re.DOTALL)
        if not match:
            raise ValueError("GeeTest returned invalid JSONP")
        payload = json.loads(match.group(1))
        if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(payload.get("data"), dict):
            raise ValueError("GeeTest returned an unsuccessful response")
        return payload["data"]

    def load_captcha(self):
        check_cancelled(self.cancel_event)
        params = {
            "captcha_id": self.captcha_id,
            "challenge": self.challenge,
            "client_type": "web",
            "lang": self.lang,
            "callback": self.callback,
        }
        res = self.session.get(
            f"{self.base_url}/load",
            params=params,
            headers=self.request_headers,
            timeout=15,
            allow_redirects=False,
        )
        check_cancelled(self.cancel_event)
        res.raise_for_status()
        return self.format_response(res.text)

    def submit_captcha(self, data: dict) -> dict:
        check_cancelled(self.cancel_event)
        self.callback = Geeked.random()

        params = {
            "callback": self.callback,
            "captcha_id": self.captcha_id,
            "client_type": "web",
            "lot_number": data["lot_number"],
            "payload": data["payload"],
            "process_token": data["process_token"],
            "payload_protocol": str(data.get("payload_protocol", "1")),
            "pt": str(data["pt"]),
            "w": Signer.generate_w(
                data,
                self.captcha_id,
                data["captcha_type"],
                session=self.session,
                cancel_event=self.cancel_event,
            ),
        }
        check_cancelled(self.cancel_event)
        verify_url = f"{self.base_url}/verify"
        request_cookies = [
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
            }
            for cookie in self.session.cookies.jar
        ]
        response = self.session.get(
            verify_url,
            params=params,
            headers=self.request_headers,
            timeout=15,
            allow_redirects=False,
        )
        check_cancelled(self.cancel_event)
        response.raise_for_status()
        if self.exchange_logger is not None:
            self.exchange_logger(
                context="GeeTest-verify",
                method="GET",
                url=verify_url,
                request_headers=self.request_headers,
                request_cookies=request_cookies,
                params=params,
                response=response,
            )
        res = self.format_response(response.text)

        if res.get("seccode") is None:
            raise CaptchaSolveRejected(res)

        return res["seccode"]

    def solve(self) -> dict:
        data = self.load_captcha()
        self.lot_number = data["lot_number"]
        seccode = self.submit_captcha(data)
        return seccode
