import requests
import datetime
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PROXY = "socks5://57.79.177.162:1010"

def test_fetch_cpc(org_code="PQ", bureau_code="PQ0200"):
    print(f"=== KIỂM TRA API EVN MIỀN TRUNG QUA PROXY SOCKS5 ===")
    print(f"Proxy: {PROXY}")
    print(f"orgCode: {org_code} | subOrgCode: {bureau_code}")

    url = "https://cskh-api.cpc.vn/api/remote/outages/area"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://cskh.cpc.vn',
        'Referer': 'https://cskh.cpc.vn/',
        'version': '1.0'
    }
    proxies = {
        'http': PROXY,
        'https': PROXY
    }

    today = datetime.datetime.now()
    from_date = today.strftime("%Y-%m-%d 00:00:00")
    to_date = (today + datetime.timedelta(days=7)).strftime("%Y-%m-%d 23:59:59")

    params = {
        'orgCode': org_code,
        'subOrgCode': bureau_code,
        'fromDate': from_date,
        'toDate': to_date,
        'page': 1,
        'limit': 100,
        'status': 'Approved'
    }

    try:
        print("\nĐang gửi yêu cầu qua proxy...")
        r = requests.get(url, headers=headers, params=params, proxies=proxies, verify=False, timeout=15)
        print(f"HTTP Status: {r.status_code}")

        if r.status_code == 200:
            data = r.json()
            items = data.get('items', [])
            if not items and isinstance(data, list):
                items = data
            print(f"✅ Thành công! {len(items)} lịch cúp điện:")
            print("-" * 50)
            for idx, it in enumerate(items[:5], 1):
                print(f"{idx}. {it.get('stationName')}")
                print(f"   {it.get('fromDateStr')} -> {it.get('toDateStr')}")
                print(f"   {it.get('reason')}")
                print("-" * 50)
            if len(items) > 5:
                print(f"... + {len(items) - 5} lịch khác.")
        else:
            print(f"❌ Thất bại! Status: {r.status_code}")
            print(f"Response: {r.text[:500]}")
    except requests.exceptions.ProxyError as e:
        print(f"❌ Lỗi proxy (có thể proxy không hoạt động): {e}")
    except requests.exceptions.ConnectTimeout:
        print(f"❌ Timeout - proxy quá chậm hoặc chặn kết nối")
    except Exception as e:
        print(f"❌ Lỗi: {e}")

if __name__ == "__main__":
    test_fetch_cpc("PQ", "PQ0200")
