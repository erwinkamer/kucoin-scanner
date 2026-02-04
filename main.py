import requests
import time

symbol = "BTCUSDTM"
url = "https://api-futures.kucoin.com/api/v1/kline/query"
end_ts = int(time.time() * 1000)
start_ts = end_ts - 50 * 3600 * 1000

params = {
    "symbol": symbol,
    "granularity": 3600,
    "from": start_ts,
    "to": end_ts
}

res = requests.get(url, params=params)
print(res.status_code)
print(res.json())
