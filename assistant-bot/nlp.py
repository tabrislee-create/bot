import requests
import json
from datetime import datetime, timedelta
import re
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "sorc/qwen3.5-instruct-uncensored:4b"
def parse_event(user_input: str) -> dict | None:
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    today_weekday = weekday_names[now.weekday()]
    prompt = f"""現在時間：{now_str}（{today_weekday}）
請從以下文字中擷取行程資訊，只回傳 JSON，不要說明、不要 markdown。
格式：{{"title": "行程名稱", "event_time": "YYYY-MM-DD HH:MM", "custom_remind_time": "YYYY-MM-DD HH:MM 或 null"}}
規則：
- 「明天」= {(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")}
- 「後天」= {(datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")}
- 「前一天」= 行程日期的前一天
- 「下週一」= 下個星期一的日期
- 「下午三點」= 15:00，「早上十點」= 10:00，「晚上八點」= 20:00
- 若無時間則預設 09:00
- custom_remind_time：若用戶有指定提醒時間（如「前一天晚上8點提醒我」），填入該提醒的完整日期時間；否則填 null
- 若無法解析則回傳 {{"error": "無法解析"}}
範例：
- 輸入「6/16 兒子畢業典禮，前一天晚上8點提醒我」今年為 {datetime.now().year} 年
  → {{"title": "兒子畢業典禮", "event_time": "{datetime.now().year}-06-16 09:00", "custom_remind_time": "{datetime.now().year}-06-15 20:00"}}
輸入：{user_input}"""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1}
        }, timeout=30)
        resp.raise_for_status()
        raw = re.sub(r"```json|```", "", resp.json().get("response", "").strip()).strip()
        data = json.loads(raw)
        if "error" in data:
            return None
        event_time = datetime.strptime(data["event_time"], "%Y-%m-%d %H:%M")
        custom_remind_time = None
        if data.get("custom_remind_time") and data["custom_remind_time"] != "null":
            try:
                custom_remind_time = datetime.strptime(data["custom_remind_time"], "%Y-%m-%d %H:%M")
            except Exception:
                pass
        return {"title": data["title"], "event_time": event_time, "custom_remind_time": custom_remind_time}
    except Exception as e:
        print(f"[nlp] parse_event error: {e}")
        return None
def parse_coffee(user_input: str) -> dict | None:
    prompt = f"""從以下文字中擷取咖啡豆資訊，只回傳 JSON，不要說明、不要 markdown。
格式：{{"roaster": "烘豆商", "product": "產品名稱", "process": "處理法", "roast_level": "烘焙度", "price": "售價"}}
規則：
- roaster：烘豆商／品牌名稱
- product：豆子名稱（產地、品種等）
- process：水洗／日曬／蜜處理／厭氧 等
- roast_level：淺焙／中淺焙／中焙／中深焙／深焙
- price：保留原始格式，如「450/磅」「250/半磅」「380元」
- 欄位若無資訊填 null
- 若完全無法解析回傳 {{"error": "無法解析"}}
輸入：{user_input}"""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1}
        }, timeout=30)
        resp.raise_for_status()
        raw = re.sub(r"```json|```", "", resp.json().get("response", "").strip()).strip()
        data = json.loads(raw)
        if "error" in data:
            return None
        return {
            "roaster":     data.get("roaster") or "",
            "product":     data.get("product") or "",
            "process":     data.get("process") or "",
            "roast_level": data.get("roast_level") or "",
            "price":       data.get("price") or "",
        }
    except Exception as e:
        print(f"[nlp] parse_coffee error: {e}")
        return None
def get_brew_params(roaster: str, product: str, process: str, roast_level: str) -> str:
    prompt = f"""你是咖啡沖煮專家，根據咖啡豆資訊給出手沖和義式的建議沖煮參數。
只回傳參數，不要多餘說明，繁體中文。

以下是輸出格式範例（衣索比亞耶加雪菲 水洗 淺焙）：

【手沖】
- 粉水比：1:15～1:16（15g 粉 / 225～240g 水）
- 水溫：91～94°C
- 研磨度：中細（顆粒感介於食鹽與細砂糖之間，約 600～800μm）
- 悶蒸：30～40 秒 / 粉重 2～3 倍水量
- 總萃取時間：2:00～2:30
- 建議風味：茉莉花香、檸檬/柑橘酸質、蜂蜜甜感、紅茶尾韻

【義式】
- 粉量：18～19g（雙份濾碗）
- 粉液比：1:2～1:2.5（18g 粉萃取 36～45g）
- 水溫：93～95°C
- 研磨度：細研磨（觸感類似細鹽與糖粉之間，約 250～400μm）
- 目標萃取時間：28～35 秒（建議含 3～5 秒低壓預浸）
- 建議風味：柑橘、花香、蜂蜜甜感、茶感尾韻

請依照上方格式，針對以下豆子給出建議：
- 烘豆商：{roaster}
- 產品：{product}
- 處理法：{process}
- 烘焙度：{roast_level}"""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.3, "num_ctx": 2048}
        }, timeout=60)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"沖煮建議取得失敗：{e}"
_TIME_PATTERN = re.compile(
    r"(明天|後天|今天|下週[一二三四五六日]|星期[一二三四五六日]|"
    r"上午|下午|早上|晚上|中午|凌晨|"
    r"\d+[點时時](\d+分)?|[一二三四五六七八九十百]+[點时時](\d+分)?)"
)
def strip_time_words(text: str) -> str:
    return _TIME_PATTERN.sub("", text).strip()
def detect_intent(user_input: str) -> dict:
    prompt = f"""判斷以下訊息的意圖，只回傳 JSON，不要說明。
格式：{{"intent": "意圖", "keyword": "關鍵字（僅 delete / coffee_query 時填）"}}
意圖選項：
- add：新增行程
- list_today：查詢今天行程
- list_week：查詢本週或近期行程
- delete：取消或刪除行程
- coffee_add：新增咖啡豆記錄（含烘豆商、豆名、處理法、烘焙度、售價等資訊）
- coffee_query：查詢咖啡豆記錄（指定烘豆商或豆名）
- unknown：其他
delete 的 keyword：只填事件名稱，不含時間日期詞。
coffee_query 的 keyword：填要查詢的烘豆商或豆名。
範例：
- 「明天下午三點看牙醫」→ {{"intent": "add", "keyword": ""}}
- 「取消明天下午三點看牙醫」→ {{"intent": "delete", "keyword": "看牙醫"}}
- 「湛盧 衣索比亞耶加雪菲 水洗 淺焙 450/磅」→ {{"intent": "coffee_add", "keyword": ""}}
- 「查咖啡 湛盧」→ {{"intent": "coffee_query", "keyword": "湛盧"}}
- 「查咖啡 耶加雪菲」→ {{"intent": "coffee_query", "keyword": "耶加雪菲"}}
輸入：{user_input}"""
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0.1}
        }, timeout=20)
        resp.raise_for_status()
        raw = re.sub(r"```json|```", "", resp.json().get("response", "").strip()).strip()
        result = json.loads(raw)
        if result.get("intent") == "delete" and result.get("keyword"):
            cleaned = strip_time_words(result["keyword"])
            if cleaned:
                result["keyword"] = cleaned
        return result
    except Exception as e:
        print(f"[nlp] detect_intent error: {e}")
        return {"intent": "unknown", "keyword": ""}
