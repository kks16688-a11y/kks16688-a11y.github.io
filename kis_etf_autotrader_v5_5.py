"""
한국투자증권 API 기반 ETF 자동매매 봇 v5.5
==============================================
[v5.5 핵심 개선 — v5.2 안정성 + v5.4 급락방어 통합]

① 인버스 진입 조건 완화
   v5.4: 기본 후보 "전부" 마이너스일 때만 인버스 허용
   v5.5: 기본 후보 "2개 이상" 마이너스일 때 인버스 허용
   → 급락 초입에 더 빠르게 방어 포지션 진입 가능

② 인버스 최대 비중 통합 관리
   v5.4: SQQQ 20% 별도 + 인버스 전체 50% (과도)
   v5.5: 인버스 전체(KODEX인버스 + SQQQ 합산) 15% 상한
   → 균형 잡힌 방어, 정방향 포지션 훼손 방지

③ SQQQ 목표비중 자체를 20%로 제한 (ChatGPT 피드백 반영)
   v5.4: 목표 50% 계산 후 20% 초과 시 스킵 → SQQQ 아예 안 사는 버그
   v5.5: 목표비중 계산 단계에서 20% 캡 적용
   → 급락장에 SQQQ 실제로 매수됨

④ KR/US 리밸런싱 상태 분리 저장 (ChatGPT 피드백 반영)
   v5.4: 일부 시장만 열려도 positions 있으면 날짜 저장
        → 한국장만 열려 KODEX200만 샀는데 QQQ를 10일 뒤에 사는 버그
   v5.5: KR/US 리밸런싱 완료 여부를 따로 저장
        → 한쪽 시장 열릴 때마다 미완료 종목 매수 시도

[v5.4 유지 사항]
   기본/방어 후보 분리 | 정방향+인버스 동시 보유 금지
   SQQQ 손절 -15% | 열린 장 기준 주문
   현금 전환 시 날짜 미저장 | 포지션 동기화

[v5.2 안정성 전부 유지]
   전체계좌 원화 통합 손실 한도 | 실계좌 포지션 동기화
   TR_ID 직접 지정 | 주간/월간 중단 자동 해제
   체결 확인 | 증거금 여유 1.30
==============================================
사전 준비:
  pip install requests pandas numpy pytz
  한국투자증권 API: https://apiportal.koreainvestment.com
"""

import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
import pytz
import json
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("autotrader_v55.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")
ET  = pytz.timezone("America/New_York")


# ══════════════════════════════════════════════════════════
# 설정값
# ══════════════════════════════════════════════════════════
CONFIG = {
    "APP_KEY":      "여기에_앱키_입력",
    "APP_SECRET":   "여기에_앱시크릿_입력",
    "ACCOUNT_NO":   "12345678",
    "ACCOUNT_PROD": "01",
    "IS_MOCK":      True,

    # 듀얼 모멘텀 파라미터
    "LOOKBACK_DAYS":  20,
    "TOP_N":          2,
    "REBALANCE_DAYS": 10,       # 달력일 기준
    "ABS_MOM_MIN":    0.0,

    # ★ v5.5: 인버스 진입 조건 임계값
    # 기본 후보(3개) 중 이 수 이상 마이너스면 인버스 허용
    "CRASH_THRESHOLD": 2,       # v5.4=3(전부), v5.5=2(2개 이상)

    # ★ v5.5: 인버스 비중 통합 관리
    "INVERSE_MAX_RATIO": 0.15,  # 인버스 전체 합산 최대 15%
    "SQQQ_MAX_RATIO":    0.20,  # SQQQ 단독 최대 20% (인버스 한도 내에서)

    # 손익 한도 (건수님 판단으로 조정)
    "TOTAL_DAILY_LOSS_LIMIT":   -0.005,  # -0.5% (조정 원하면 -0.015 ~ -0.02)
    "TOTAL_WEEKLY_LOSS_LIMIT":  -0.03,
    "TOTAL_MONTHLY_LOSS_LIMIT": -0.05,
    "DAILY_PROFIT_TARGET":      None,
    "MAX_CONSECUTIVE_LOSS":     2,

    # 종목별 손절
    "SYMBOL_LOSS_LIMIT": -0.07,
    "SQQQ_LOSS_LIMIT":   -0.15,

    # 레버리지 비중
    "LEVERAGE_ETF_MAX_RATIO": 0.30,

    # 안전장치
    "COOLDOWN_SECONDS": 1800,

    # API
    "CACHE_TTL":          60,
    "ORDER_RETRY":        3,
    "ORDER_RETRY_DELAY":  2,
    "ORDER_CONFIRM_WAIT": 3,
    "POSITION_FILE":      "positions_v55.json",
    "STATE_FILE":         "state_v55.json",
    "CHECK_INTERVAL":     300,
}

# 기본/방어 후보 분리
BASE_ETFS = {
    "KODEX200": {"code": "069500", "market": "KR", "type": "base"},
    "SPY":      {"code": "SPY",    "market": "US", "type": "base"},
    "QQQ":      {"code": "QQQ",    "market": "US", "type": "base"},
}
DEFENSE_ETFS = {
    "KODEX인버스": {"code": "114800", "market": "KR", "type": "inverse"},
    "SQQQ":       {"code": "SQQQ",   "market": "US", "type": "inverse"},
}
ALL_ETFS = {**BASE_ETFS, **DEFENSE_ETFS}

KR_ETFS = {n: v for n, v in ALL_ETFS.items() if v["market"] == "KR"}
US_ETFS = {n: v for n, v in ALL_ETFS.items() if v["market"] == "US"}

CONFLICT_PAIRS = [
    ("KODEX200", "KODEX인버스"),
    ("SPY",  "SQQQ"),
    ("QQQ",  "SQQQ"),
]

LEVERAGE_CODES = {"122630", "233740", "TQQQ", "SQQQ"}
INVERSE_CODES  = {"114800", "SQQQ"}

REAL_URL = "https://openapi.koreainvestment.com:9443"
MOCK_URL  = "https://openapivts.koreainvestment.com:29443"


# ══════════════════════════════════════════════════════════
# 장 시간
# ══════════════════════════════════════════════════════════
def is_kr_market_open() -> bool:
    now = datetime.now(KST)
    if now.weekday() >= 5: return False
    t = now.strftime("%H%M")
    return "0900" <= t < "1530"

def is_us_market_open() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5: return False
    o = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    c = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return o <= now < c

def check_time_filter_kr() -> bool:
    t = datetime.now(KST).strftime("%H%M")
    return "0900" <= t <= "0910" or "1510" <= t <= "1530"


# ══════════════════════════════════════════════════════════
# 영속화
# ══════════════════════════════════════════════════════════
def save_positions(pos: dict):
    with open(CONFIG["POSITION_FILE"], "w", encoding="utf-8") as f:
        json.dump(pos, f, ensure_ascii=False, indent=2)

def load_positions() -> dict:
    if os.path.exists(CONFIG["POSITION_FILE"]):
        with open(CONFIG["POSITION_FILE"], "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(CONFIG["STATE_FILE"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_state() -> dict:
    if os.path.exists(CONFIG["STATE_FILE"]):
        with open(CONFIG["STATE_FILE"], "r", encoding="utf-8") as f:
            return json.load(f)
    # ★ v5.5: KR/US 리밸런싱 상태 분리
    return {
        "last_rebalance_date": "",
        "rebal_kr_done": False,
        "rebal_us_done": False,
        "rebal_target":  [],
    }


# ══════════════════════════════════════════════════════════
# KIS API
# ══════════════════════════════════════════════════════════
class KISApi:
    def __init__(self):
        self.base_url     = MOCK_URL if CONFIG["IS_MOCK"] else REAL_URL
        self.app_key      = CONFIG["APP_KEY"]
        self.app_secret   = CONFIG["APP_SECRET"]
        self.account_no   = CONFIG["ACCOUNT_NO"]
        self.account_prod = CONFIG["ACCOUNT_PROD"]
        self.access_token  = None
        self.token_expired = None
        self._cache:      dict = {}
        self._cache_time: dict = {}

    def get_token(self):
        url  = f"{self.base_url}/oauth2/tokenP"
        body = {"grant_type": "client_credentials",
                "appkey": self.app_key, "appsecret": self.app_secret}
        res  = requests.post(url, json=body)
        res.raise_for_status()
        data = res.json()
        self.access_token  = data["access_token"]
        self.token_expired = datetime.now() + timedelta(hours=23)
        log.info("✅ 토큰 발급 완료")

    def _ensure_token(self):
        if not self.access_token or datetime.now() >= self.token_expired:
            self.get_token()

    def _headers(self, tr_id: str) -> dict:
        self._ensure_token()
        return {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
            "tr_id":         tr_id,
        }

    def _get_cache(self, key):
        t = self._cache_time.get(key)
        if t and (datetime.now() - t).total_seconds() < CONFIG["CACHE_TTL"]:
            return self._cache.get(key)
        return None

    def _set_cache(self, key, val):
        self._cache[key] = val
        self._cache_time[key] = datetime.now()

    def invalidate_cache(self):
        self._cache.clear()
        self._cache_time.clear()

    def _order_with_retry(self, fn, ticker) -> bool:
        for attempt in range(1, CONFIG["ORDER_RETRY"] + 1):
            try:
                if fn():
                    self.invalidate_cache()
                    return True
                log.warning(f"[{ticker}] 주문 실패 {attempt}회")
            except Exception as e:
                log.error(f"[{ticker}] 주문 예외: {e}")
            time.sleep(CONFIG["ORDER_RETRY_DELAY"])
        log.error(f"[{ticker}] ❌ 전체 실패")
        return False

    def get_usd_krw(self) -> float:
        cached = self._get_cache("usd_krw")
        if cached: return cached
        try:
            url    = f"{self.base_url}/uapi/overseas-stock/v1/quotations/inquire-daily-chartprice"
            params = {"FID_COND_MRKT_DIV_CODE":"X","FID_INPUT_ISCD":"FX@KRW",
                      "FID_INPUT_DATE_1":datetime.now().strftime("%Y%m%d"),
                      "FID_INPUT_DATE_2":datetime.now().strftime("%Y%m%d"),
                      "FID_PERIOD_DIV_CODE":"D"}
            res  = requests.get(url, headers=self._headers("FHKST03030100"),
                                params=params, timeout=5)
            data = res.json()
            if data.get("rt_cd") == "0" and data.get("output2"):
                rate = float(data["output2"][0].get("ovrs_nmix_prpr", 1350))
                self._set_cache("usd_krw", rate)
                return rate
        except Exception:
            pass
        return 1350.0

    def get_total_asset_kr(self) -> int:
        cached = self._get_cache("total_kr")
        if cached is not None: return cached
        url    = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "AFHR_FLPR_YN":"N","OFL_YN":"N","INQR_DVSN":"02","UNPR_DVSN":"01",
                  "FUND_STTL_ICLD_YN":"N","FNCG_AMT_AUTO_RDPT_YN":"N",
                  "PRCS_DVSN":"00","CTX_AREA_FK100":"","CTX_AREA_NK100":""}
        tr_id = "TTTC8434R" if not CONFIG["IS_MOCK"] else "VTTC8434R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        val   = 0
        if data.get("rt_cd") == "0":
            o2 = data.get("output2", [{}])
            val = int(o2[0].get("tot_evlu_amt", 0)) if o2 else 0
        self._set_cache("total_kr", val)
        return val

    def get_total_asset_us(self) -> float:
        cached = self._get_cache("total_us")
        if cached is not None: return cached
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "OVRS_EXCG_CD":"NASD","TR_CRCY_CD":"USD",
                  "CTX_AREA_FK200":"","CTX_AREA_NK200":""}
        tr_id = "TTTS3012R" if not CONFIG["IS_MOCK"] else "VTTS3012R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        val   = 0.0
        if data.get("rt_cd") == "0":
            o2 = data.get("output2", [{}])
            val = float(o2[0].get("tot_evlu_amt", 0)) if o2 else 0.0
        self._set_cache("total_us", val)
        return val

    def get_total_asset_krw(self) -> int:
        return self.get_total_asset_kr() + int(self.get_total_asset_us() * self.get_usd_krw())

    def get_cash_kr(self) -> int:
        url    = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-psbl-order"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "PDNO":"005930","ORD_UNPR":"0","ORD_QTY":"0",
                  "OVRS_ICLD_YN":"N","CMA_EVLU_AMT_ICLD_YN":"N"}
        tr_id = "TTTC8908R" if not CONFIG["IS_MOCK"] else "VTTC8908R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        return int(data["output"]["ord_psbl_cash"]) if data.get("rt_cd") == "0" else 0

    def get_cash_us(self) -> float:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-psamount"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "OVRS_EXCG_CD":"NASD","TR_CRCY_CD":"USD"}
        tr_id = "TTTS3007R" if not CONFIG["IS_MOCK"] else "VTTS3007R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        return float(data["output"]["ovrs_ord_psbl_amt"]) if data.get("rt_cd") == "0" else 0.0

    def get_kr_price(self, code) -> int:
        cached = self._get_cache(f"p_kr_{code}")
        if cached: return cached
        url    = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE":"J","FID_INPUT_ISCD":code}
        res    = requests.get(url, headers=self._headers("FHKST01010100"), params=params)
        data   = res.json()
        val    = int(data["output"]["stck_prpr"]) if data.get("rt_cd") == "0" else 0
        if val: self._set_cache(f"p_kr_{code}", val)
        return val

    def get_us_price(self, symbol) -> float:
        cached = self._get_cache(f"p_us_{symbol}")
        if cached: return cached
        url    = f"{self.base_url}/uapi/overseas-stock/v1/quotations/price"
        excd   = "NAS" if symbol in ["QQQ","TQQQ","SQQQ"] else "NYS"
        params = {"AUTH":"","EXCD":excd,"SYMB":symbol}
        res    = requests.get(url, headers=self._headers("HHDFS00000300"), params=params)
        data   = res.json()
        val    = float(data["output"]["last"]) if data.get("rt_cd") == "0" else 0.0
        if val: self._set_cache(f"p_us_{symbol}", val)
        return val

    def get_kr_daily(self, code, days=30) -> pd.DataFrame:
        url    = f"{self.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        params = {"FID_COND_MRKT_DIV_CODE":"J","FID_INPUT_ISCD":code,
                  "FID_PERIOD_DIV_CODE":"D","FID_ORG_ADJ_PRC":"1"}
        res    = requests.get(url, headers=self._headers("FHKST01010400"), params=params)
        data   = res.json()
        if data.get("rt_cd") == "0":
            df = pd.DataFrame(data.get("output", []))
            if df.empty: return pd.DataFrame()
            df = df[["stck_bsop_date","stck_clpr"]].rename(
                columns={"stck_bsop_date":"date","stck_clpr":"close"})
            df["close"] = df["close"].astype(float)
            return df.sort_values("date").reset_index(drop=True).tail(days)
        return pd.DataFrame()

    def get_us_daily(self, symbol, days=30) -> pd.DataFrame:
        url    = f"{self.base_url}/uapi/overseas-stock/v1/quotations/inquire-daily-chartprice"
        excd   = "NAS" if symbol in ["QQQ","TQQQ","SQQQ"] else "NYS"
        params = {"AUTH":"","EXCD":excd,"SYMB":symbol,
                  "GUBN":"0","BYMD":datetime.now().strftime("%Y%m%d"),"MODP":"1"}
        res    = requests.get(url, headers=self._headers("HHDFS76240000"), params=params)
        data   = res.json()
        if data.get("rt_cd") == "0":
            df = pd.DataFrame(data.get("output2", []))
            if df.empty: return pd.DataFrame()
            df = df[["xymd","clos"]].rename(columns={"xymd":"date","clos":"close"})
            df["close"] = df["close"].astype(float)
            return df.sort_values("date").reset_index(drop=True).tail(days)
        return pd.DataFrame()

    def _order_kr_raw(self, code, qty, side) -> bool:
        url   = f"{self.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        tr_id = {"BUY":  "TTTC0802U" if not CONFIG["IS_MOCK"] else "VTTC0802U",
                 "SELL": "TTTC0801U" if not CONFIG["IS_MOCK"] else "VTTC0801U"}[side]
        body  = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                 "PDNO":code,"ORD_DVSN":"01","ORD_QTY":str(qty),"ORD_UNPR":"0"}
        res   = requests.post(url, headers=self._headers(tr_id), json=body)
        data  = res.json(); ok = data.get("rt_cd") == "0"
        log.info(f"[국내 {side}] {code} {qty}주 → {'✅' if ok else '❌ '+data.get('msg1','')}")
        return ok

    def _order_us_raw(self, symbol, qty, side) -> bool:
        url   = f"{self.base_url}/uapi/overseas-stock/v1/trading/order"
        excd  = "NASD" if symbol in ["QQQ","TQQQ","SQQQ"] else "NYSE"
        tr_id = ("TTTT1002U" if not CONFIG["IS_MOCK"] else "VTTT1002U") if side == "BUY" \
                else ("TTTT1006U" if not CONFIG["IS_MOCK"] else "VTTT1006U")
        body  = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                 "OVRS_EXCG_CD":excd,"PDNO":symbol,"ORD_QTY":str(qty),
                 "OVRS_ORD_UNPR":"0","ORD_SVR_DVSN_CD":"0","ORD_DVSN":"00"}
        if side == "SELL": body["SLL_TYPE"] = "00"
        res   = requests.post(url, headers=self._headers(tr_id), json=body)
        data  = res.json(); ok = data.get("rt_cd") == "0"
        log.info(f"[미국 {side}] {symbol} {qty}주 → {'✅' if ok else '❌ '+data.get('msg1','')}")
        return ok

    def confirm_kr_position(self, code) -> tuple:
        time.sleep(CONFIG["ORDER_CONFIRM_WAIT"])
        self.invalidate_cache()
        url    = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "AFHR_FLPR_YN":"N","OFL_YN":"N","INQR_DVSN":"02","UNPR_DVSN":"01",
                  "FUND_STTL_ICLD_YN":"N","FNCG_AMT_AUTO_RDPT_YN":"N",
                  "PRCS_DVSN":"00","CTX_AREA_FK100":"","CTX_AREA_NK100":""}
        tr_id = "TTTC8434R" if not CONFIG["IS_MOCK"] else "VTTC8434R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        if data.get("rt_cd") == "0":
            for item in data.get("output1", []):
                if item.get("pdno") == code:
                    return int(item.get("hldg_qty", 0)), float(item.get("pchs_avg_pric", 0))
        return 0, 0.0

    def confirm_us_position(self, symbol) -> tuple:
        time.sleep(CONFIG["ORDER_CONFIRM_WAIT"])
        self.invalidate_cache()
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "OVRS_EXCG_CD":"NASD","TR_CRCY_CD":"USD",
                  "CTX_AREA_FK200":"","CTX_AREA_NK200":""}
        tr_id = "TTTS3012R" if not CONFIG["IS_MOCK"] else "VTTS3012R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        if data.get("rt_cd") == "0":
            for item in data.get("output1", []):
                if item.get("ovrs_pdno") == symbol:
                    return int(item.get("ovrs_cblc_qty", 0)), float(item.get("pchs_avg_pric", 0))
        return 0, 0.0

    def sync_all_positions(self) -> dict:
        result = {}
        url    = f"{self.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "AFHR_FLPR_YN":"N","OFL_YN":"N","INQR_DVSN":"02","UNPR_DVSN":"01",
                  "FUND_STTL_ICLD_YN":"N","FNCG_AMT_AUTO_RDPT_YN":"N",
                  "PRCS_DVSN":"00","CTX_AREA_FK100":"","CTX_AREA_NK100":""}
        tr_id = "TTTC8434R" if not CONFIG["IS_MOCK"] else "VTTC8434R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        if data.get("rt_cd") == "0":
            for item in data.get("output1", []):
                code = item.get("pdno"); qty = int(item.get("hldg_qty", 0))
                avg  = float(item.get("pchs_avg_pric", 0))
                name = next((n for n, v in KR_ETFS.items() if v["code"] == code), None)
                if name and qty > 0:
                    result[name] = {"code":code,"qty":qty,"avg_price":avg,"market":"KR"}
                    log.info(f"[동기화] {name} {qty}주 @ {avg:,.0f}원")
        url    = f"{self.base_url}/uapi/overseas-stock/v1/trading/inquire-balance"
        params = {"CANO":self.account_no,"ACNT_PRDT_CD":self.account_prod,
                  "OVRS_EXCG_CD":"NASD","TR_CRCY_CD":"USD",
                  "CTX_AREA_FK200":"","CTX_AREA_NK200":""}
        tr_id = "TTTS3012R" if not CONFIG["IS_MOCK"] else "VTTS3012R"
        res   = requests.get(url, headers=self._headers(tr_id), params=params)
        data  = res.json()
        if data.get("rt_cd") == "0":
            for item in data.get("output1", []):
                sym = item.get("ovrs_pdno"); qty = int(item.get("ovrs_cblc_qty", 0))
                avg = float(item.get("pchs_avg_pric", 0))
                if sym in US_ETFS and qty > 0:
                    result[sym] = {"code":sym,"qty":qty,"avg_price":avg,"market":"US"}
                    log.info(f"[동기화] {sym} {qty}주 @ ${avg:.2f}")
        return result


# ══════════════════════════════════════════════════════════
# 듀얼 모멘텀 트레이더 v5.5
# ══════════════════════════════════════════════════════════
class DualMomentumTrader:
    def __init__(self):
        self.api = KISApi()
        self.start_total_krw  = 0
        self.weekly_start     = 0
        self.monthly_start    = 0
        self.daily_stopped    = False
        self.weekly_stopped   = False
        self.monthly_stopped  = False
        self.consecutive_loss = 0
        self.week_key  = ""
        self.month_key = ""
        self.positions:      dict = {}
        self.cooldown_until: dict = {}
        self.order_banned:   set  = set()
        self.state = load_state()
        self._last_init_date = ""

    def init_day(self):
        self.daily_stopped    = False
        self.consecutive_loss = 0
        self.api.invalidate_cache()
        self.start_total_krw = self.api.get_total_asset_krw()
        real  = self.api.sync_all_positions()
        saved = load_positions()
        self.positions = {}
        for name, pos in real.items():
            self.positions[name] = {**pos, **{k: v for k, v in saved.get(name, {}).items()
                                              if k not in pos}}
        now = datetime.now(KST)
        wk, mo = now.strftime("%Y-%W"), now.strftime("%Y-%m")
        if wk != self.week_key:
            self.week_key = wk; self.weekly_start = self.start_total_krw
            self.weekly_stopped = False
            log.info(f"📅 새 주 | 기준: {self.weekly_start:,}원")
        if mo != self.month_key:
            self.month_key = mo; self.monthly_start = self.start_total_krw
            self.monthly_stopped = False
            log.info(f"📅 새 달 | 기준: {self.monthly_start:,}원")
        log.info(f"=== 당일 시작 | 전체자산: {self.start_total_krw:,}원 ===")

    def check_period_reset(self):
        now = datetime.now(KST)
        wk, mo = now.strftime("%Y-%W"), now.strftime("%Y-%m")
        if self.weekly_stopped and wk != self.week_key:
            log.info("📅 새 주 → 주간 중단 해제")
            self.week_key = wk; self.weekly_start = self.api.get_total_asset_krw()
            self.weekly_stopped = False
        if self.monthly_stopped and mo != self.month_key:
            log.info("📅 새 달 → 월간 중단 해제")
            self.month_key = mo; self.monthly_start = self.api.get_total_asset_krw()
            self.monthly_stopped = False

    def check_limits(self) -> str:
        if self.daily_stopped:   return "STOPPED"
        if self.weekly_stopped:  return "WEEKLY_STOPPED"
        if self.monthly_stopped: return "MONTHLY_STOPPED"
        self.api.invalidate_cache()
        cur = self.api.get_total_asset_krw()
        pnl = (cur - self.start_total_krw) / self.start_total_krw if self.start_total_krw else 0
        log.info(f"📊 전체자산: {cur:,}원 | 손익: {pnl:+.2%}")
        if CONFIG["DAILY_PROFIT_TARGET"] is not None and pnl >= CONFIG["DAILY_PROFIT_TARGET"]:
            return "TAKE_PROFIT"
        if pnl <= CONFIG["TOTAL_DAILY_LOSS_LIMIT"]:
            self.consecutive_loss += 1
            log.warning(f"⛔ 일일 손절 ({pnl:+.2%}) | 연속: {self.consecutive_loss}회")
            if self.consecutive_loss >= CONFIG["MAX_CONSECUTIVE_LOSS"]:
                self.daily_stopped = True; return "STOPPED"
            return "STOP_LOSS"
        if self.weekly_start and \
           (cur-self.weekly_start)/self.weekly_start <= CONFIG["TOTAL_WEEKLY_LOSS_LIMIT"]:
            self.weekly_stopped = True; log.warning("🚫 주간 손실"); return "WEEKLY_STOPPED"
        if self.monthly_start and \
           (cur-self.monthly_start)/self.monthly_start <= CONFIG["TOTAL_MONTHLY_LOSS_LIMIT"]:
            self.monthly_stopped = True; log.warning("🚫 월간 손실"); return "MONTHLY_STOPPED"
        return "OK"

    # ══════════════════════════════════════════════════════
    # ★ v5.5: KR/US 분리 리밸런싱 판단
    # ══════════════════════════════════════════════════════
    def should_rebalance(self) -> bool:
        """
        전체 리밸런싱 주기 도래 여부 판단.
        주기 도래 시 KR/US 완료 상태 초기화.
        """
        last = self.state.get("last_rebalance_date", "")
        if not last:
            return True
        days = (datetime.now(KST).date() - pd.to_datetime(last).date()).days
        log.info(f"  마지막 리밸런싱: {last} ({days}일 경과)")
        if days >= CONFIG["REBALANCE_DAYS"]:
            # 주기 도래 → KR/US 완료 상태 초기화
            self.state["rebal_kr_done"] = False
            self.state["rebal_us_done"] = False
            self.state["rebal_target"]  = []
            return True
        return False

    def has_pending_orders(self) -> bool:
        """
        주기 미도래지만 미완료 시장이 있는지 확인.
        (한쪽 장만 열려 있어서 일부만 처리된 경우 재시도)
        """
        target = self.state.get("rebal_target", [])
        if not target:
            return False
        kr_done = self.state.get("rebal_kr_done", False)
        us_done = self.state.get("rebal_us_done", False)
        kr_needed = any(ALL_ETFS[n]["market"] == "KR" for n in target)
        us_needed = any(ALL_ETFS[n]["market"] == "US" for n in target)
        pending = (kr_needed and not kr_done) or (us_needed and not us_done)
        if pending:
            log.info(f"  미완료 주문 있음 → KR완료={kr_done} US완료={us_done} 대상={target}")
        return pending

    # ══════════════════════════════════════════════════════
    # ★ v5.5: 인버스 진입 조건 (2개 이상 마이너스)
    # ══════════════════════════════════════════════════════
    def calc_momentum_all(self) -> tuple:
        """
        반환: (base_moms, all_base_moms, defense_moms, is_crash)
        base_moms:     절대모멘텀 통과한 기본 후보 {name: mom}
        all_base_moms: 전체 기본 후보 모멘텀 {name: mom} (부호 포함)
        defense_moms:  절대모멘텀 통과한 방어 후보 {name: mom}
        is_crash:      기본 후보 CRASH_THRESHOLD개 이상 마이너스
        """
        lb = CONFIG["LOOKBACK_DAYS"]
        base_moms     = {}
        all_base_moms = {}
        defense_moms  = {}

        log.info("📈 모멘텀 계산...")
        for name, info in ALL_ETFS.items():
            try:
                if info["market"] == "KR":
                    df = self.api.get_kr_daily(info["code"], days=lb + 5)
                else:
                    df = self.api.get_us_daily(info["code"], days=lb + 5)

                if df.empty or len(df) < lb + 1:
                    log.warning(f"  [{name}] 데이터 부족")
                    continue

                cur  = df["close"].iloc[-1]
                past = df["close"].iloc[-(lb + 1)]
                if past <= 0: continue
                mom = (cur - past) / past
                log.info(f"  [{name}] 모멘텀: {mom:+.2%}")

                if info["type"] == "base":
                    all_base_moms[name] = mom          # 부호 무관 전부 기록
                    if mom > CONFIG["ABS_MOM_MIN"]:
                        base_moms[name] = mom
                else:
                    if mom > CONFIG["ABS_MOM_MIN"]:
                        defense_moms[name] = mom

                time.sleep(0.2)
            except Exception as e:
                log.error(f"  [{name}] 오류: {e}")

        # ★ v5.5: 기본 후보 중 마이너스 개수로 급락 판단
        neg_count = sum(1 for m in all_base_moms.values() if m <= CONFIG["ABS_MOM_MIN"])
        is_crash  = neg_count >= CONFIG["CRASH_THRESHOLD"]
        log.info(f"  기본 후보 마이너스: {neg_count}개 / 임계값: {CONFIG['CRASH_THRESHOLD']}개 "
                 f"→ 급락={'YES' if is_crash else 'NO'}")
        return base_moms, all_base_moms, defense_moms, is_crash

    def select_top(self, base_moms: dict, defense_moms: dict,
                   is_crash: bool, total_krw: int) -> dict:
        """
        반환: {name: target_krw} — 종목별 목표 금액
        ★ v5.5: SQQQ 목표비중 자체를 20%로 캡 적용
        ★ v5.5: 인버스 전체 15% 상한 적용
        """
        N = CONFIG["TOP_N"]

        if not is_crash:
            # 정상장: 기본 후보에서만 TOP N
            candidates = sorted(base_moms, key=base_moms.get, reverse=True)[:N]
            log.info(f"🏆 정상장 선택: {candidates}")
            alloc = total_krw / len(candidates) if candidates else 0
            result = {n: alloc for n in candidates}
        else:
            # 급락장: 방어 후보 허용
            if defense_moms:
                candidates = sorted(defense_moms, key=defense_moms.get, reverse=True)[:N]
                log.info(f"🛡️ 급락장 방어 선택 (후보): {candidates}")

                # ★ v5.5: 인버스 비중 통합 15% 상한 적용
                inverse_budget = int(total_krw * CONFIG["INVERSE_MAX_RATIO"])
                sqqq_budget    = int(total_krw * CONFIG["SQQQ_MAX_RATIO"])

                result = {}
                remaining_budget = inverse_budget
                for name in candidates:
                    if remaining_budget <= 0:
                        log.info(f"  [{name}] 인버스 한도 소진 → 스킵")
                        break
                    if name == "SQQQ":
                        # SQQQ는 인버스 한도 내에서 20% 캡 추가 적용
                        alloc = min(remaining_budget, sqqq_budget)
                    else:
                        alloc = min(remaining_budget,
                                    int(total_krw / len(candidates)))
                    result[name] = alloc
                    remaining_budget -= alloc
                    log.info(f"  [{name}] 목표금액: {alloc:,}원 "
                             f"(남은 인버스예산: {remaining_budget:,}원)")
            else:
                log.info("💰 방어 후보도 없음 → 전량 현금")
                result = {}

        # 정방향 + 인버스 동시 보유 금지
        result = self._remove_conflicts(result)
        return result

    def _remove_conflicts(self, target: dict) -> dict:
        to_remove = set()
        for base_n, inv_n in CONFLICT_PAIRS:
            if base_n in target and inv_n in target:
                log.warning(f"⚠️ 충돌 감지: {base_n} + {inv_n} → {inv_n} 제거")
                to_remove.add(inv_n)
        return {n: v for n, v in target.items() if n not in to_remove}

    def _filter_by_open_market(self, target: dict) -> dict:
        kr_open = is_kr_market_open()
        us_open = is_us_market_open()
        filtered = {}
        for name, alloc in target.items():
            market = ALL_ETFS[name]["market"]
            if market == "KR" and kr_open:
                filtered[name] = alloc
            elif market == "US" and us_open:
                filtered[name] = alloc
            elif not kr_open and market == "KR":
                log.info(f"  [{name}] 국내장 닫힘 → 주문 보류")
            elif not us_open and market == "US":
                log.info(f"  [{name}] 미국장 닫힘 → 주문 보류")
        return filtered

    # ══════════════════════════════════════════════════════
    # ★ v5.5: 리밸런싱 (KR/US 분리 저장)
    # ══════════════════════════════════════════════════════
    def rebalance(self, use_saved_target: bool = False):
        log.info("🔄 리밸런싱 시작")
        total_krw = self.api.get_total_asset_krw()
        rate      = self.api.get_usd_krw()
        BAND      = 0.05

        # 목표 종목 결정
        if use_saved_target and self.state.get("rebal_target"):
            # 미완료 주문 재시도 — 저장된 target 사용
            saved_target = self.state["rebal_target"]
            # 저장된 목표를 비중으로 복원 (균등 배분)
            alloc = total_krw / len(saved_target) if saved_target else 0
            target_alloc = {}
            for name in saved_target:
                if name in ALL_ETFS:
                    if ALL_ETFS[name]["type"] == "inverse":
                        if name == "SQQQ":
                            target_alloc[name] = min(alloc,
                                int(total_krw * CONFIG["SQQQ_MAX_RATIO"]))
                        else:
                            target_alloc[name] = min(alloc,
                                int(total_krw * CONFIG["INVERSE_MAX_RATIO"]))
                    else:
                        target_alloc[name] = alloc
            log.info(f"  미완료 재시도 | 대상: {saved_target}")
        else:
            # 신규 리밸런싱
            base_moms, all_base_moms, defense_moms, is_crash = self.calc_momentum_all()
            target_alloc = self.select_top(base_moms, defense_moms, is_crash, total_krw)

            # 목표 종목 저장 (미완료 대비)
            self.state["rebal_target"]  = list(target_alloc.keys())
            self.state["rebal_kr_done"] = False
            self.state["rebal_us_done"] = False
            save_state(self.state)

        # 목표 외 청산 (열린 장만)
        for name in list(self.positions.keys()):
            if name not in target_alloc:
                info = ALL_ETFS.get(name, {})
                if info.get("market") == "KR" and not is_kr_market_open():
                    log.info(f"  [{name}] 국내장 닫힘 → 청산 보류"); continue
                if info.get("market") == "US" and not is_us_market_open():
                    log.info(f"  [{name}] 미국장 닫힘 → 청산 보류"); continue
                log.info(f"  [{name}] 모멘텀 탈락 → 청산")
                self._sell(name)

        if not target_alloc:
            log.info("  목표 없음 → 날짜 미저장")
            return

        # 열린 장 필터
        orderable = self._filter_by_open_market(target_alloc)

        # 매수 / 비중 조정
        kr_ordered = []
        us_ordered = []
        for name, alloc in orderable.items():
            if name in self.order_banned: continue
            if self.is_in_cooldown(name):  continue
            if not self._check_leverage_limit(name, alloc): continue

            info = ALL_ETFS[name]
            if name in self.positions:
                pos = self.positions[name]
                if info["market"] == "KR":
                    cur_val = pos["qty"] * self.api.get_kr_price(info["code"])
                else:
                    cur_val = int(pos["qty"] * self.api.get_us_price(info["code"]) * rate)
                diff_ratio = (cur_val - alloc) / total_krw
                if diff_ratio > BAND:
                    self._partial_sell(name, cur_val - alloc)
                elif diff_ratio < -BAND:
                    self._buy(name, alloc - cur_val)
                else:
                    log.info(f"  [{name}] 비중 OK → 유지")
            else:
                self._buy(name, alloc)

            if info["market"] == "KR":
                kr_ordered.append(name)
            else:
                us_ordered.append(name)

        # ★ v5.5: KR/US 완료 상태 분리 저장
        target_names = self.state.get("rebal_target", [])
        kr_targets = [n for n in target_names if ALL_ETFS.get(n, {}).get("market") == "KR"]
        us_targets = [n for n in target_names if ALL_ETFS.get(n, {}).get("market") == "US"]

        if kr_targets and is_kr_market_open() and kr_ordered:
            self.state["rebal_kr_done"] = True
            log.info(f"  ✅ KR 리밸런싱 완료: {kr_ordered}")
        if us_targets and is_us_market_open() and us_ordered:
            self.state["rebal_us_done"] = True
            log.info(f"  ✅ US 리밸런싱 완료: {us_ordered}")

        # 목표 종목 전체 완료됐을 때만 날짜 저장
        kr_done = self.state.get("rebal_kr_done", False) or not kr_targets
        us_done = self.state.get("rebal_us_done", False) or not us_targets

        if self.positions and kr_done and us_done:
            self.state["last_rebalance_date"] = datetime.now(KST).strftime("%Y-%m-%d")
            save_state(self.state)
            log.info(f"✅ 리밸런싱 전체 완료 | 보유: {list(self.positions.keys())}")
        elif self.positions:
            save_state(self.state)
            log.info(f"⏳ 리밸런싱 일부 완료 | KR={kr_done} US={us_done} | "
                     f"날짜 저장 보류 (미완료 시장 대기)")
        else:
            log.info("  포지션 없음 → 날짜 미저장")

    # ══════════════════════════════════════════════════════
    # 비중 체크
    # ══════════════════════════════════════════════════════
    def _get_leverage_ratio(self) -> float:
        total = self.api.get_total_asset_krw()
        if total == 0: return 0.0
        rate = self.api.get_usd_krw(); lev = 0
        for name, pos in self.positions.items():
            code = ALL_ETFS.get(name, {}).get("code", "")
            if code not in LEVERAGE_CODES: continue
            p = self.api.get_kr_price(code) if pos["market"] == "KR" \
                else int(self.api.get_us_price(code) * rate)
            lev += pos["qty"] * p
        return lev / total

    def _check_leverage_limit(self, name, alloc_krw) -> bool:
        code = ALL_ETFS.get(name, {}).get("code", "")
        if code not in LEVERAGE_CODES: return True
        total = self.api.get_total_asset_krw()
        if total == 0: return False
        if self._get_leverage_ratio() + alloc_krw / total > CONFIG["LEVERAGE_ETF_MAX_RATIO"]:
            log.info(f"⚠️ [{name}] 레버리지 한도 초과 → 스킵")
            return False
        return True

    # ══════════════════════════════════════════════════════
    # 종목별 손절
    # ══════════════════════════════════════════════════════
    def check_symbol_stops(self):
        for name in list(self.positions.keys()):
            pos  = self.positions[name]
            info = ALL_ETFS.get(name, {})
            if info.get("market") == "KR" and not is_kr_market_open(): continue
            if info.get("market") == "US" and not is_us_market_open(): continue
            p = self.api.get_kr_price(pos["code"]) if pos["market"] == "KR" \
                else self.api.get_us_price(pos["code"])
            if pos["avg_price"] > 0 and p > 0:
                pnl   = (p - pos["avg_price"]) / pos["avg_price"]
                limit = CONFIG["SQQQ_LOSS_LIMIT"] if name == "SQQQ" \
                        else CONFIG["SYMBOL_LOSS_LIMIT"]
                if pnl <= limit:
                    log.warning(f"⛔ [{name}] 손절 ({pnl:+.2%} ≤ {limit:.0%})")
                    self._sell(name)

    # ══════════════════════════════════════════════════════
    # 쿨다운
    # ══════════════════════════════════════════════════════
    def is_in_cooldown(self, name) -> bool:
        until = self.cooldown_until.get(name)
        if until and datetime.now() < until:
            rem = int((until - datetime.now()).total_seconds() / 60)
            log.info(f"❄️ [{name}] 쿨다운 {rem}분")
            return True
        return False

    def set_cooldown(self, name):
        self.cooldown_until[name] = datetime.now() + timedelta(seconds=CONFIG["COOLDOWN_SECONDS"])

    # ══════════════════════════════════════════════════════
    # 매수 / 매도
    # ══════════════════════════════════════════════════════
    def _buy(self, name: str, alloc_krw: float):
        info = ALL_ETFS[name]
        if info["market"] == "KR":
            code   = info["code"]
            price  = self.api.get_kr_price(code)
            cash   = self.api.get_cash_kr()
            invest = min(int(alloc_krw), cash)
            qty    = int(invest / (price * 1.30)) if price > 0 else 0
            if qty <= 0:
                log.warning(f"[{name}] 매수 수량 부족"); return
            ok = self.api._order_with_retry(
                lambda c=code, q=qty: self.api._order_kr_raw(c, q, "BUY"), name)
            if ok:
                rq, ra = self.api.confirm_kr_position(code)
                if rq > 0:
                    self.positions[name] = {"code":code,"qty":rq,"avg_price":ra,"market":"KR"}
                    save_positions(self.positions)
                    log.info(f"✅ [{name}] {rq}주 @ {ra:,.0f}원")
            else:
                self.order_banned.add(name)
        else:
            price      = self.api.get_us_price(name)
            cash_us    = self.api.get_cash_us()
            rate       = self.api.get_usd_krw()
            invest_usd = min(alloc_krw / rate, cash_us)
            qty        = int(invest_usd / (price * 1.001)) if price > 0 else 0
            if qty <= 0:
                log.warning(f"[{name}] 매수 수량 부족"); return
            ok = self.api._order_with_retry(
                lambda s=name, q=qty: self.api._order_us_raw(s, q, "BUY"), name)
            if ok:
                rq, ra = self.api.confirm_us_position(name)
                if rq > 0:
                    self.positions[name] = {"code":name,"qty":rq,"avg_price":ra,"market":"US"}
                    save_positions(self.positions)
                    log.info(f"✅ [{name}] {rq}주 @ ${ra:.2f}")
            else:
                self.order_banned.add(name)

    def _partial_sell(self, name: str, excess_krw: float):
        pos  = self.positions.get(name)
        if not pos: return
        info = ALL_ETFS[name]
        if info["market"] == "KR":
            price    = self.api.get_kr_price(pos["code"])
            sell_qty = min(int(excess_krw / price), pos["qty"]) if price > 0 else 0
        else:
            price    = self.api.get_us_price(pos["code"])
            rate     = self.api.get_usd_krw()
            sell_qty = min(int(excess_krw / (price * rate)), pos["qty"]) if price > 0 else 0
        if sell_qty <= 0: return
        log.info(f"  [{name}] 비중 초과 → {sell_qty}주 일부 매도")
        if info["market"] == "KR":
            ok = self.api._order_with_retry(
                lambda c=pos["code"], q=sell_qty: self.api._order_kr_raw(c, q, "SELL"), name)
            if ok:
                rq, ra = self.api.confirm_kr_position(pos["code"])
                if rq > 0:
                    self.positions[name]["qty"] = rq
                    self.positions[name]["avg_price"] = ra
                else:
                    self.positions.pop(name, None)
                save_positions(self.positions)
        else:
            ok = self.api._order_with_retry(
                lambda s=name, q=sell_qty: self.api._order_us_raw(s, q, "SELL"), name)
            if ok:
                rq, ra = self.api.confirm_us_position(name)
                if rq > 0:
                    self.positions[name]["qty"] = rq
                    self.positions[name]["avg_price"] = ra
                else:
                    self.positions.pop(name, None)
                save_positions(self.positions)

    def _sell(self, name: str):
        pos = self.positions.get(name)
        if not pos or pos["qty"] <= 0: return
        info = ALL_ETFS[name]
        if info["market"] == "KR":
            ok = self.api._order_with_retry(
                lambda c=pos["code"], q=pos["qty"]: self.api._order_kr_raw(c, q, "SELL"), name)
        else:
            ok = self.api._order_with_retry(
                lambda s=name, q=pos["qty"]: self.api._order_us_raw(s, q, "SELL"), name)
        if ok:
            self.positions.pop(name, None)
            save_positions(self.positions)
            self.set_cooldown(name)
        else:
            self.order_banned.add(name)

    def liquidate_all(self):
        for name in list(self.positions.keys()):
            self._sell(name)

    # ══════════════════════════════════════════════════════
    # 메인 루프
    # ══════════════════════════════════════════════════════
    def run(self):
        self.api.get_token()
        log.info("🤖 듀얼 모멘텀 봇 v5.5 시작")

        while True:
            now = datetime.now(KST)
            self.check_period_reset()

            if self.monthly_stopped:
                log.warning("이번 달 거래 중단"); time.sleep(3600); continue
            if self.weekly_stopped:
                log.warning("이번 주 거래 중단"); time.sleep(3600); continue

            kr_open = is_kr_market_open()
            us_open = is_us_market_open()

            if not kr_open and not us_open:
                log.info(f"⏳ 장 대기 ({now.strftime('%H:%M')} KST)")
                time.sleep(60); continue

            today = now.strftime("%Y-%m-%d")
            if self._last_init_date != today:
                self.init_day()
                self._last_init_date = today

            status = self.check_limits()
            if status in ("STOPPED","WEEKLY_STOPPED","MONTHLY_STOPPED"):
                self.liquidate_all(); time.sleep(300); continue
            if status in ("TAKE_PROFIT","STOP_LOSS"):
                self.liquidate_all(); time.sleep(300); continue

            if kr_open and check_time_filter_kr():
                log.info("⏰ 장 초반/마감 시간대 → 스킵")
                time.sleep(60); continue

            self.check_symbol_stops()

            if self.should_rebalance():
                self.rebalance(use_saved_target=False)
            elif self.has_pending_orders():
                # ★ v5.5: 미완료 시장 열리면 재시도
                self.rebalance(use_saved_target=True)

            time.sleep(CONFIG["CHECK_INTERVAL"])


# ══════════════════════════════════════════════════════════
# 실행
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════════╗
║         KIS ETF 자동매매 봇 v5.5 — 듀얼 모멘텀              ║
╠══════════════════════════════════════════════════════════════╣
║  [v5.5 핵심 개선]                                            ║
║  ① 인버스 진입 조건: 기본 후보 2개 이상 마이너스            ║
║  ② 인버스 비중 통합 15% 상한 (KODEX인버스+SQQQ 합산)        ║
║  ③ SQQQ 목표비중 20% 캡 (계산 단계에서 적용)                ║
║  ④ KR/US 리밸런싱 완료 분리 저장                            ║
║     → 한쪽 장만 열려도 부분 완료 후 나머지 대기             ║
║     → 전체 완료 시에만 리밸런싱 날짜 저장                   ║
╠══════════════════════════════════════════════════════════════╣
║  [안전장치]                                                  ║
║  전체 -0.5% / 주간 -3% / 월간 -5% | 종목별 -7% (-15%)      ║
║  주문 3회 재시도 + 체결 확인 | 매도 후 30분 쿨다운           ║
╚══════════════════════════════════════════════════════════════╝

⚠️  IS_MOCK = True 상태로 최소 1개월 검증 후 실전 전환!
⚠️  일일 손절 -0.5% 기준 — 완화 원할 시 TOTAL_DAILY_LOSS_LIMIT 조정
""")
    confirm = input("시작하시겠습니까? (y/n): ").strip().lower()
    if confirm == "y":
        trader = DualMomentumTrader()
        trader.run()
    else:
        print("취소되었습니다.")
