import requests
import datetime
import urllib3

# Tắt các cảnh báo SSL không an toàn của urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def test_fetch_cpc(org_code="PQ", bureau_code="PQ0200"):
    print(f"=== ĐANG KIỂM TRA KẾT NỐI API EVN MIỀN TRUNG ===")
    print(f"Đơn vị quản lý (orgCode): {org_code}")
    print(f"Mã Điện lực (subOrgCode): {bureau_code}")
    
    url = "https://cskh-api.cpc.vn/api/remote/outages/area"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0',
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://cskh.cpc.vn',
        'Referer': 'https://cskh.cpc.vn/',
        'version': '1.0'
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
        print("\nĐang gửi yêu cầu lấy lịch cúp điện...")
        r = requests.get(url, headers=headers, params=params, verify=False, timeout=15)
        print(f"Trạng thái HTTP phản hồi: {r.status_code}")
        
        if r.status_code == 200:
            data = r.json()
            items = data.get('items', [])
            if not items and isinstance(data, list):
                items = data
                
            print(f"✅ Thành công! Tìm thấy {len(items)} lịch cúp điện dự kiến:")
            print("-" * 50)
            for idx, it in enumerate(items[:5], 1):
                print(f"{idx}. Địa điểm: {it.get('stationName')}")
                print(f"   Thời gian: {it.get('fromDateStr')} -> {it.get('toDateStr')}")
                print(f"   Lý do: {it.get('reason')}")
                print("-" * 50)
            if len(items) > 5:
                print(f"... và {len(items) - 5} lịch cúp điện khác.")
        else:
            print(f"❌ Thất bại! Mã lỗi HTTP: {r.status_code}")
            print(f"Phản hồi từ server: {r.text[:500]}")
            
    except Exception as e:
        print(f"❌ Đã xảy ra lỗi khi kết nối:")
        print(e)

if __name__ == "__main__":
    # Mặc định test cho Khánh Hòa (PQ, PQ0200)
    # Bạn có thể đổi sang: Quảng Trị (PC02, PC02AA) hoặc khu vực khác
    test_fetch_cpc("PQ", "PQ0200")
