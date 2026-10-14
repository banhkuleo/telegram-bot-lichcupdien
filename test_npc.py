import requests
import datetime
import urllib3
import sys
from bs4 import BeautifulSoup

# Ép buộc console output sử dụng UTF-8 trên Windows
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Tắt cảnh báo SSL không an sau
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def get_npc_bureaus(macty="PA22"):
    url = "https://cskh.npc.com.vn/ThongTinKhachHang/GetDienLuc"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest'
    }
    params = {'macty': macty}
    
    try:
        r = requests.get(url, headers=headers, params=params, verify=False, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            options = soup.find_all('option')
            bureaus = {}
            for opt in options:
                val = opt.get('value')
                text = opt.text.strip()
                if val and val.strip() and "chọn" not in text.lower():
                    bureaus[val.strip()] = text
            return bureaus
        else:
            return None
    except Exception as e:
        return None

def fetch_npc_outages(madvi, tu_ngay=None, den_ngay=None):
    today = datetime.datetime.now()
    if not tu_ngay:
        tu_ngay = today.strftime("%d/%m/%Y")
    if not den_ngay:
        den_ngay = (today + datetime.timedelta(days=15)).strftime("%d/%m/%Y")
        
    url = "https://cskh.npc.com.vn/ThongTinKhachHang/LichNgungGiamCungCapDienSPC"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
        'X-Requested-With': 'XMLHttpRequest'
    }
    params = {
        'madvi': madvi,
        'tuNgay': tu_ngay,
        'denNgay': den_ngay
    }
    
    try:
        r = requests.get(url, headers=headers, params=params, verify=False, timeout=15)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, 'html.parser')
            table = soup.find('table')
            if not table:
                print("No table found")
                return
                
            tbody = table.find('tbody')
            rows = tbody.find_all('tr') if tbody else table.find_all('tr')[1:]
            
            non_empty_bureau = 0
            non_empty_date = 0
            non_empty_reason = 0
            
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 6:
                    continue
                
                bureau = cells[0].text.strip()
                date = cells[1].text.strip()
                reason = cells[5].text.strip()
                
                if bureau:
                    non_empty_bureau += 1
                if date:
                    non_empty_date += 1
                if reason:
                    non_empty_reason += 1
                    
            print(f"Tổng số dòng: {len(rows)}")
            print(f"Số dòng có Điện lực không rỗng: {non_empty_bureau}")
            print(f"Số dòng có Ngày không rỗng: {non_empty_date}")
            print(f"Số dòng có Lý do không rỗng: {non_empty_reason}")
            
            # In dòng đầu tiên có lý do không rỗng
            for row in rows:
                cells = row.find_all('td')
                if len(cells) >= 6 and cells[5].text.strip():
                    print("\nVí dụ dòng có Lý do:")
                    for idx, c in enumerate(cells):
                        print(f"Cell {idx}: '{c.text.strip()}'")
                    break
        else:
            print(f"Error {r.status_code}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    bureaus = get_npc_bureaus("PA22")
    if bureaus:
        first_bureau_code = list(bureaus.keys())[0]
        fetch_npc_outages(first_bureau_code)
