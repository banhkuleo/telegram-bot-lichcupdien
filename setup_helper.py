import urllib.request
import re
import json
import ssl

def get_companies():
    url = "https://www.cskh.evnspc.vn/TraCuu/LichNgungGiamCungCapDien"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    
    print("Connecting to EVN SPC CSKH...")
    # Ignore SSL verification issues if any
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ctx) as response:
            html = response.read().decode('utf-8')
            
            # Find the select dropdown with ID 'idCongTyDienLuc'
            # <select ... id="idCongTyDienLuc" ...> ... </select>
            match = re.search(r'<select[^>]*id="idCongTyDienLuc"[^>]*>(.*?)</select>', html, re.DOTALL)
            if not match:
                match = re.search(r'<select[^>]*name="idCongTyDienLuc"[^>]*>(.*?)</select>', html, re.DOTALL)
                
            if match:
                select_content = match.group(1)
                # Find all <option value="CODE">NAME</option>
                options = re.findall(r'<option[^>]*value="([^"]+)"[^>]*>\s*(.*?)\s*</option>', select_content)
                
                companies = {}
                for val, text in options:
                    if val and val.strip() and val != "0" and val != "":
                        companies[val.strip()] = text.strip()
                
                if companies:
                    with open("companies.json", "w", encoding="utf-8") as f:
                        json.dump(companies, f, ensure_ascii=False, indent=4)
                    print(f"Successfully created companies.json with {len(companies)} companies!")
                    for k, v in companies.items():
                        print(f"  {k}: {v}")
                    return True
                else:
                    print("No valid option elements found in idCongTyDienLuc select.")
            else:
                print("Could not find select element with id/name 'idCongTyDienLuc' in HTML.")
    except Exception as e:
        print(f"Error occurred while fetching EVN SPC: {e}")
    
    # Pre-fallback static list of known Southern Power companies in case fetch fails
    print("Falling back to static EVN SPC company codes list...")
    fallback = {
        "PB01": "Công ty Điện lực Bình Dương",
        "PB02": "Công ty Điện lực Cần Thơ",
        "PB03": "Công ty Điện lực Đồng Nai",
        "PB04": "Công ty Điện lực Bà Rịa - Vũng Tàu",
        "PB05": "Công ty Điện lực Bình Phước",
        "PB06": "Công ty Điện lực Tây Ninh",
        "PB07": "Công ty Điện lực Long An",
        "PB08": "Công ty Điện lực Tiền Giang",
        "PB09": "Công ty Điện lực Bến Tre",
        "PB10": "Công ty Điện lực Vĩnh Long",
        "PB11": "Công ty Điện lực Trà Vinh",
        "PB12": "Công ty Điện lực Đồng Tháp",
        "PB13": "Công ty Điện lực An Giang",
        "PB14": "Công ty Điện lực Kiên Giang",
        "PB15": "Công ty Điện lực Hậu Giang",
        "PB16": "Công ty Điện lực Sóc Trăng",
        "PB17": "Công ty Điện lực Bạc Liêu",
        "PB18": "Công ty Điện lực Cà Mau",
        "PB19": "Công ty Điện lực Lâm Đồng",
        "PB20": "Công ty Điện lực Ninh Thuận",
        "PB21": "Công ty Điện lực Bình Thuận"
    }
    with open("companies.json", "w", encoding="utf-8") as f:
        json.dump(fallback, f, ensure_ascii=False, indent=4)
    print("Successfully created companies.json using fallback list!")
    return False

if __name__ == "__main__":
    get_companies()
