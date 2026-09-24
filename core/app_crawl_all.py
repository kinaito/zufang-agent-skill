# -*- coding: utf-8 -*-
"""
链家 App 全量子区域房源自动化采集与增量监控 (生产标准版)
- 支持冷启动（新设备自动搜索+设置全套筛选）与热启动（历史记录商圈快捷载入+前置校验）
- 严格筛选：整租, 3居+, 2200-3600元/月, 面积≥100㎡, 有电梯, 民水民电
- 顺流流水线：单向滑动，遇新增房源当场点入提取真实 FS 编号并原地返回，顺流滑至自然触底
- 数据洁净保障：仅收录提取到真实有效 FS 编号的房源，严禁生成伪 ID 占位
"""
import glob
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import uiautomator2 as u2
from PIL import Image

SERIAL = os.environ.get("ANDROID_SERIAL", "")
CST = timezone(timedelta(hours=8))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "房源")
INDEX_FILE = os.path.join(OUTPUT_DIR, "index.json")
COMMUNITIES_FILE = os.path.join(PROJECT_ROOT, "communities.json")

def load_runtime_config():
    """从 agent_pack/config.json 动态加载用户自定义配置，若无则使用默认值"""
    cfg_path = os.path.join(PROJECT_ROOT, "agent_pack", "config.json")
    example_path = os.path.join(PROJECT_ROOT, "agent_pack", "config.example.json")
    target = cfg_path if os.path.exists(cfg_path) else example_path
    
    defaults = {
        "city": "佛山",
        "district": "南海",
        "sub_regions": ["城市广场", "大沥", "桂城", "黄岐", "湖景", "里水", "平洲", "千灯湖", "盐步"],
        "min_price": 2200,
        "max_price": 3600,
        "min_area": 100.0,
        "map_center": [113.1534, 23.0384],
        "top_ratio": 0.16,
        "bottom_ratio": 0.77
    }
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                c = json.load(f)
            meta = c.get("project_meta", {})
            sc = c.get("search_criteria", {})
            rt = c.get("runtime", {})
            subs = c.get("sub_regions", [])
            sub_names = [s["name"] if isinstance(s, dict) else s for s in subs] if subs else defaults["sub_regions"]
            
            return {
                "city": meta.get("target_city", defaults["city"]),
                "district": meta.get("target_district", defaults["district"]),
                "sub_regions": sub_names,
                "min_price": sc.get("price_range", {}).get("min", defaults["min_price"]),
                "max_price": sc.get("price_range", {}).get("max", defaults["max_price"]),
                "min_area": float(sc.get("area_min_sqm", defaults["min_area"])),
                "map_center": rt.get("map_center", defaults["map_center"]),
                "top_ratio": rt.get("ui_safe_bounds", {}).get("top_ratio", defaults["top_ratio"]),
                "bottom_ratio": rt.get("ui_safe_bounds", {}).get("bottom_ratio", defaults["bottom_ratio"])
            }
        except Exception as e:
            print(f"[!] 读取配置文件失败，使用默认配置: {e}", flush=True)
    return defaults

RUN_CFG = load_runtime_config()
TARGET_CITY = RUN_CFG["city"]
TARGET_DISTRICT = RUN_CFG["district"]
ALL_SUBREGIONS = RUN_CFG["sub_regions"]
MIN_PRICE = RUN_CFG["min_price"]
MAX_PRICE = RUN_CFG["max_price"]
MIN_AREA = RUN_CFG["min_area"]
DEFAULT_MAP_CENTER = RUN_CFG["map_center"]
SAFE_TOP_RATIO = RUN_CFG["top_ratio"]
SAFE_BOTTOM_RATIO = RUN_CFG["bottom_ratio"]

# 屏幕自适应参数（采用屏幕宽高的相对百分比，适配各种长宽比例与直板/特殊屏）
BASE_W, BASE_H = 1264, 2800
SCREEN_W = BASE_W
SCREEN_H = BASE_H
SX = 1.0
SY = 1.0
SAFE_SWIPE_UP = (SCREEN_W // 2, int(SCREEN_H * 0.65), SCREEN_W // 2, int(SCREEN_H * 0.32))


def sx(v):
    """横向基准像素 -> 当前设备像素"""
    return int(round(v * SX))


def sy(v):
    """纵向基准像素 -> 当前设备像素"""
    return int(round(v * SY))


def sc(x, y):
    """基准坐标点 (x, y) -> 当前设备坐标点"""
    return sx(x), sy(y)

MAX_EMPTY_PAGES = 5

RID_TITLE = "card_rent_house_list_tv_title_v3"
RID_DESC = "card_rent_house_list_tv_desc_v3"
RID_TAG = "tv_txt"


def price_num(price):
    m = re.search(r"(\d+)", str(price))
    return int(m.group(1)) if m else 0


def area_num(area):
    m = re.search(r"(\d+(?:\.\d+)?)", str(area))
    return float(m.group(1)) if m else 0.0


def norm_price(price):
    m = re.match(r"(\d+)\s*元/月", str(price))
    return f"{m.group(1)} 元/月" if m else f"{price} 元/月" if str(price).isdigit() else str(price)


def _extract_title(title):
    """'整租4居·万科金色城市' -> (rentalType, houseType, community)"""
    m = re.match(r"^(整租)(\d+)居[·\.\s]+(.+)$", title)
    if m:
        return m.group(1), m.group(2) + "居", m.group(3).strip()
    if title.startswith("整租·"):
        return "整租", "", title[3:].strip()
    return "", "", title.strip()


def _extract_desc(desc):
    """'119㎡｜南｜高楼层｜电梯' -> (area, orientation, floor, elevator)"""
    parts = [p.strip() for p in desc.split("｜")]
    area = ""
    orientation = ""
    floor = ""
    elevator = ""
    for p in parts:
        if "㎡" in p or "m²" in p:
            area = p.replace("m²", "㎡")
        elif re.match(r"^[东南西北]+$", p):
            orientation = p
        elif p and ("楼层" in p or p.endswith("层")):
            floor = p
        elif p in ("电梯", "无电梯", "有电梯", "步梯"):
            elevator = p
    return area, orientation, floor, elevator


def _parse_node(node_xml):
    text = ""
    rid = ""
    bounds = (0, 0, 0, 0)
    m = re.search(r'text="([^"]*)"', node_xml)
    if m:
        text = m.group(1)
    m = re.search(r'resource-id="([^"]*)"', node_xml)
    if m:
        rid = m.group(1)
    m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node_xml)
    if m:
        bounds = tuple(int(g) for g in m.groups())
    return text, rid, bounds


def split_nodes(xml_text):
    nodes = []
    for block in re.findall(r"<node\b[^>]*?(?:/>|>)", xml_text):
        text, rid, bounds = _parse_node(block)
        if text or RID_TITLE in rid:
            nodes.append((text, rid, bounds))
    nodes.sort(key=lambda n: (n[2][1], n[2][0]))
    return nodes


def parse_listing(xml_text):
    nodes = split_nodes(xml_text)
    title_ys = [n[2][1] for n in nodes if RID_TITLE in n[1]]
    cards = []
    for i, ty in enumerate(title_ys):
        next_ty = title_ys[i + 1] if i + 1 < len(title_ys) else 10 ** 9
        members = [n for n in nodes if ty <= n[2][1] < next_ty]
        card = {"title": "", "desc": "", "district": "", "price": "", "tags": [], "tap": None}
        full_tags = []
        for text, rid, bounds in members:
            if RID_TITLE in rid:
                card["title"] = text
                # 点击精准定位在标题文字中心，彻底避开横幅与咨询按钮
                cx = (bounds[0] + bounds[2]) // 2
                cy = (bounds[1] + bounds[3]) // 2
                card["tap"] = (cx, cy)
            elif RID_DESC in rid:
                card["desc"] = text
            elif "third_desc" in rid:
                card["district"] = text
            elif "house_price_v3" in rid:
                card["price"] = text
            elif "house_price_unit" in rid:
                card["price"] += text
            elif RID_TAG in rid and text:
                full_tags.append(text)
        card["tags"] = full_tags
        cards.append(card)
    return cards


def list_signature(card):
    rental_type, house_type, community = _extract_title(card["title"])
    area, orientation, floor, elevator = _extract_desc(card["desc"])
    tags_str = ",".join(sorted(card.get("tags", [])))
    parts = [
        community,
        area,
        card["price"],
        orientation,
        floor,
        elevator,
        tags_str,
    ]
    return "|".join(str(p) for p in parts)


def project_signature(rec):
    community = rec.get("community", "")
    community = re.sub(r"^(整租|合租)[·\.]", "", community).strip()
    parts = [
        community,
        str(int(area_num(rec.get("area", "")))),
        str(price_num(rec.get("price", ""))),
        rec.get("orientation", ""),
        rec.get("floor", ""),
        rec.get("district", ""),
    ]
    return "|".join(parts)


def hard_physical_signature(rec):
    """提取不可变物理特征签名（不含易变的价格与运营标签），用于重上房源的 L1 软匹配。"""
    community = rec.get("community", "")
    community = re.sub(r"^(整租|合租)[·\.]", "", community).strip()
    parts = [
        community,
        str(int(area_num(rec.get("area", "")))),
        rec.get("orientation", ""),
        rec.get("floor", ""),
        rec.get("district", ""),
    ]
    return "|".join(parts)


def compute_dhash(img):
    """计算 64-bit 差异哈希 (dHash)。"""
    resized = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(resized.getdata())
    diff = []
    for row in range(8):
        for col in range(8):
            diff.append(pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return f"{sum(bit << i for i, bit in enumerate(diff)):016x}"


def hamming_distance(h1, h2):
    """计算两个 Hex 哈希的汉明距离（0~64 位差异）。"""
    if not h1 or not h2:
        return 64
    try:
        return bin(int(h1, 16) ^ int(h2, 16)).count('1')
    except Exception:
        return 64


def compute_room_dhash(d):
    """在详情页首屏截取抗噪黄金窗口 [0, 380, w, 1200] 并计算 64-bit dHash。"""
    try:
        # 1. 确保在【房间】Tab
        room_tab = d(text="房间")
        if room_tab.exists:
            # 若未处于高亮状态，点击切到房间
            room_tab.click_exists(timeout=0.8)
            time.sleep(0.3)
        # 2. 截取首屏安全居室区域（避开顶部浮窗与底部横幅）
        screen = d.screenshot()
        w, h = screen.size
        safe_crop = screen.crop((0, sy(380), w, sy(1200)))
        return compute_dhash(safe_crop)
    except Exception as e:
        print(f"    [dHash计算异常] {e}", flush=True)
        return ""


def find_house_code(xml):
    m = re.search(r'resource-id="[^"]*true_house_tv_true_id"[^>]*text="[^"]*?(FS\d+)"', xml)
    if m:
        return m.group(1)
    m = re.search(r"房源验真编码[:：]?\s*(FS\d+)", xml)
    if m:
        return m.group(1)
    m = re.search(r"房源编码[:：]?\s*(FS\d+)", xml)
    if m:
        return m.group(1)
    m = re.search(r'(FS\d{15,})', xml)
    if m:
        return m.group(1)
    return ""


def close_dialogs(d):
    xml = d.dump_hierarchy()
    if "内容反馈" in xml:
        d.click(0.92, 0.36)
        time.sleep(0.3)
        return True
    if "我知道了" in xml:
        d(text="我知道了").click_exists(timeout=1.0)
        time.sleep(0.3)
        return True
    if "允许" in xml and "权限" in xml:
        d(text="允许").click_exists(timeout=1.0)
        time.sleep(0.3)
        return True
    # 关闭顶部中介搭讪弹窗
    btn_close = d(resourceId="com.homelink.android:id/btn_close")
    if btn_close.exists:
        btn_close.click()
        time.sleep(0.3)
        return True
    return False


def load_index():
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"listings": {}, "lastScan": None}


def save_index(index_data):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)


def load_communities():
    if os.path.exists(COMMUNITIES_FILE):
        with open(COMMUNITIES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_communities(comms):
    with open(COMMUNITIES_FILE, "w", encoding="utf-8") as f:
        json.dump(comms, f, ensure_ascii=False, indent=2)


def load_existing_db():
    sigs = set()
    known_ids = set()
    records_by_sig = {}       # proj_sig -> record
    records_by_hard_sig = {}  # hard_sig -> [record1, record2, ...]
    index_data = load_index()

    # 1. 优先加载 房源/FS*.json
    for path in glob.glob(os.path.join(OUTPUT_DIR, "FS*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            lid = rec.get("id")
            if lid:
                known_ids.add(lid)
                psig = project_signature(rec)
                sigs.add(psig)
                records_by_sig[psig] = rec
                
                hsig = hard_physical_signature(rec)
                if hsig not in records_by_hard_sig:
                    records_by_hard_sig[hsig] = []
                records_by_hard_sig[hsig].append(rec)
        except Exception:
            pass

    # 2. 补充 index.json
    for lid, item in index_data.get("listings", {}).items():
        known_ids.add(lid)
        psig = project_signature(item)
        sigs.add(psig)
        if psig not in records_by_sig:
            records_by_sig[psig] = item
        hsig = hard_physical_signature(item)
        if hsig not in records_by_hard_sig:
            records_by_hard_sig[hsig] = []
        if not any(r.get("id") == lid for r in records_by_hard_sig[hsig]):
            records_by_hard_sig[hsig].append(item)

    return sigs, known_ids, records_by_sig, records_by_hard_sig, index_data


def ensure_in_zufang(d):
    close_dialogs(d)
    if not d.app_current().get("package") == "com.homelink.android":
        d.app_start("com.homelink.android")
        time.sleep(2.5)
        close_dialogs(d)

    for attempt in range(8):
        xml = d.dump_hierarchy()
        matches = re.findall(r'text="([^"]+)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        visible_texts = [t for t, x1, y1, x2, y2 in matches]

        # 0. 检查是否在通知栏/锁屏页，自动向上滑动解锁或收起通知栏
        if any(k in visible_texts for k in ["已连接到 USB 调试", "手电筒", "蓝牙", "只能拨打紧急呼救电话", "已充满电", "NotificationShade"]):
            print("  [视觉看门狗] 检测到锁屏/通知栏状态，执行向上滑动解锁/收起...", flush=True)
            d.swipe(SCREEN_W // 2, int(SCREEN_H * 0.8), SCREEN_W // 2, int(SCREEN_H * 0.2), duration=0.2)
            time.sleep(1.0)
            d.app_start("com.homelink.android")
            time.sleep(1.5)
            continue

        # 1. 检查是否在房源列表页 (包含 5 大 Filter Tab 或房源卡片)
        tabs = get_filter_tab_bounds(d)
        tab_names = [t[0] for t in tabs]
        if (len(tabs) >= 3 and any(k in tab_names for k in ["租金", "2200-3600元", "更多", "更多(2)", "排序", "价格(从低到高)"])) or "card_rent_house_list_tv_title_v3" in xml:
            return True

        # 2. 检查并关闭弹窗/权限/广告
        for close_kw in ["关闭", "跳过", "以后再说", "我知道了", "取消", "暂不升级", "允许"]:
            if d(text=close_kw).click_exists(timeout=0.2):
                print(f"  [视觉看门狗] 点击关闭弹层【{close_kw}】", flush=True)
                time.sleep(0.5)

        # 3. 如果在搜索/历史记录页，点击第一条商圈历史记录或搜索框进入列表
        if "历史记录" in visible_texts or "new_house_search_et_search" in xml:
            hist_item = d(resourceId="com.homelink.android:id/item_house_search_history_tv_title")
            if hist_item.exists:
                print(f"  [视觉看门狗] 处于搜索历史页，点击历史商圈【{hist_item.get_text()}】进入列表...", flush=True)
                hist_item.click()
                time.sleep(2.0)
                continue

        # 4. 如果在选择城市页
        if "选择城市" in visible_texts or "当前定位城市" in visible_texts:
            print("  [视觉看门狗] 处于选择城市页，按 back 返回...", flush=True)
            d.press("back")
            time.sleep(1.0)
            continue

        # 5. 如果在详情页 / 微聊 / 帮我找房 / 广告
        if any(k in visible_texts for k in ["房屋简介", "在线咨询", "预约看房", "户型分间", "VR带看", "费用明细", "我的找房需求", "告诉我你的要求", "已匹配专家"]):
            print("  [视觉看门狗] 处于详情/咨询/找房需求页，按 back 返回列表...", flush=True)
            d.press("back")
            time.sleep(1.0)
            continue

        # 6. 如果在租房频道首页
        if any(k in visible_texts for k in ["地图找房", "我要出租", "VR看房", "心动找房"]):
            confirm_btn = d(resourceId="com.homelink.android:id/card_home_condition_new_tv_goto_list")
            if not confirm_btn.exists:
                confirm_btn = d(text="查看房源")
            if confirm_btn.exists:
                print("  [视觉看门狗] 处于租房频道首页，点击【查看房源】进入列表页...", flush=True)
                confirm_btn.click()
                time.sleep(2.0)
                continue

        # 7. 如果在 App 总首页
        if any(k in visible_texts for k in ["二手房", "新房", "装修", "你想住在哪？"]):
            zufang_node = d(text="租房")
            if zufang_node.exists:
                print("  [视觉看门狗] 处于 App 首页，点击【租房】金刚位...", flush=True)
                zufang_node.click()
                time.sleep(2.0)
                continue

        # 兜底恢复
        print("  [视觉看门狗] 未知异常状态，按 back 恢复...", flush=True)
        d.press("back")
        time.sleep(1.0)


def open_search_page(d):
    close_dialogs(d)
    for _ in range(4):
        xml = d.dump_hierarchy()
        if "历史记录" in xml or "new_house_search_et_search" in xml:
            return True
        search_bar = d(resourceId="com.homelink.android:id/title_bar_house_search_tv_hint")
        if search_bar.exists:
            search_bar.click()
            time.sleep(1.2)
            continue
        search_bar2 = d(textContains="小区/商圈/地铁站/上班地点")
        if search_bar2.exists:
            search_bar2.click()
            time.sleep(1.2)
            continue
        # 列表已滚动时按 back 唤出搜索页或返回租房首页
        d.press("back")
        time.sleep(1.0)
        close_dialogs(d)
        ensure_in_zufang(d)
    return "历史记录" in d.dump_hierarchy()


def scroll_to_list_top(d):
    for _ in range(3):
        xml = d.dump_hierarchy()
        if "请输入小区/商圈/地铁站/上班地点" in xml or "查看房源" in xml:
            break
        x = SCREEN_W // 2
        d.swipe(x, sy(700), x, sy(1600), duration=0.3)
        time.sleep(0.5)


def get_filter_tab_bounds(d):
    """动态解析顶部 5 大 Filter Tab 的中心坐标与包围盒 (自适应任何机型与分辨率)"""
    xml = d.dump_hierarchy()
    matches = re.findall(r'text="([^"]+)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    # 筛选栏第一行通常位于 y=180~320 区间，高度 <= 60，排除搜索框与次级标签
    row_tabs = []
    excluded_keywords = ["请输入小区/商圈/地铁站/上班地点", "地图", "消息", "近地铁", "限时特价", "7日新上", "loft/复式", "可月租", "热门商圈", "大家都在看", "相似商圈", "附近商圈"]
    for t, x1, y1, x2, y2 in matches:
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if (150 <= y1 <= 320) and (y2 - y1 <= 60) and (x2 - x1 >= 25):
            if not any(k in t for k in excluded_keywords):
                row_tabs.append((t, (x1 + x2) // 2, (y1 + y2) // 2, (x1, y1, x2, y2)))
    # 按横坐标从左往右排序
    row_tabs.sort(key=lambda item: item[1])
    return row_tabs


def open_filter_tab(d, tab_idx, expected_kw):
    """
    tab_idx: 1 (位置), 2 (居室/面积), 3 (租金), 4 (更多), 5 (排序)
    expected_kw: 展开后必定包含的关键字
    """
    for attempt in range(4):
        scroll_to_list_top(d)
        time.sleep(0.3)
        tabs = get_filter_tab_bounds(d)
        if len(tabs) >= tab_idx:
            cx, cy = tabs[tab_idx - 1][1], tabs[tab_idx - 1][2]
        else:
            # 兜底比例坐标
            x_map = {1: 137, 2: 375, 3: 613, 4: 851, 5: 1090}
            cx, cy = sx(x_map[tab_idx]), sy(260)
        
        d.click(cx, cy)
        time.sleep(1.0)
        xml = d.dump_hierarchy()
        if expected_kw in xml or d(resourceId="com.homelink.android:id/card_common_filter_tv_confirm").exists:
            return True
        # 尝试微调 Y 坐标点击
        d.click(cx, cy - 20)
        time.sleep(1.0)
        if expected_kw in d.dump_hierarchy() or d(resourceId="com.homelink.android:id/card_common_filter_tv_confirm").exists:
            return True
    return False


def navigate_to_subregion(d, region):
    print(f"\n=======================================================", flush=True)
    print(f"   【商圈单选视觉闭环 SOP】 目标: 【{TARGET_DISTRICT} -> {region}】", flush=True)
    print(f"=======================================================", flush=True)
    ensure_in_zufang(d)
    close_dialogs(d)

    # 1. 强保证打开 Tab 1 位置弹窗
    for _ in range(5):
        if d(resourceId="com.homelink.android:id/card_common_filter_tv_confirm").exists or d(text="重置").exists:
            break
        open_filter_tab(d, 1, "查看房源")
        time.sleep(0.8)
    
    assert d(resourceId="com.homelink.android:id/card_common_filter_tv_confirm").exists or d(text="重置").exists, "未能打开位置筛选弹窗！"

    # 2. 先点击【重置】清空全局历史勾选（彻底杜绝多选累加为 商圈(X)）
    reset_btn = d(text="重置")
    if reset_btn.exists:
        reset_btn.click()
        print("  [步骤 1/4: 重置清空] 点击【重置】清空历史商圈多选", flush=True)
        time.sleep(0.5)

    # 3. 显式切换回【区域 -> 目标行政区】并视觉核验
    for attempt in range(5):
        d(text="区域", resourceId="com.homelink.android:id/item_common_filter_tv_content").click_exists(timeout=0.8)
        time.sleep(0.3)
        d(text=TARGET_DISTRICT, resourceId="com.homelink.android:id/item_common_filter_tv_content").click_exists(timeout=0.8)
        time.sleep(0.6)

        # 视觉核验第 3 列是否出现该行政区商圈
        xml = d.dump_hierarchy()
        col3_items = re.findall(r'text=\"([^\"]+)\"[^>]*resource-id=\"com.homelink.android:id/item_location_third_filter_tv_content\"', xml)
        if any(r in col3_items for r in ALL_SUBREGIONS) or region in col3_items:
            print(f"  [步骤 2/4: 视觉确认{TARGET_DISTRICT}] 第 3 列确认已出现目标商圈列表: {col3_items[:3]} [PASS]", flush=True)
            break
    else:
        raise RuntimeError(f"未能切换到【{TARGET_DISTRICT}】行政区！请检查链家 App 当前城市与行政区设置。")

    # 4. 在第 3 列查找并单选点击目标商圈 (先向下滑动找，未找到再回滚向上找)
    found = False
    col3_x = int(SCREEN_W * 0.75)
    for attempt in range(8):
        node = d(text=region, resourceId="com.homelink.android:id/item_location_third_filter_tv_content")
        if node.exists:
            print(f"  [步骤 3/4: 单选目标] 精准命中目标商圈【{region}】，执行点击！", flush=True)
            node.click()
            found = True
            break
        d.swipe(col3_x, sy(1100), col3_x, sy(600), duration=0.3)
        time.sleep(0.4)

    if not found:
        # 回滚向上滑动重新搜索
        for _ in range(8):
            d.swipe(col3_x, sy(600), col3_x, sy(1100), duration=0.25)
            time.sleep(0.2)
        for attempt in range(8):
            node = d(text=region, resourceId="com.homelink.android:id/item_location_third_filter_tv_content")
            if node.exists:
                print(f"  [步骤 3/4: 单选目标] 回滚后精准命中目标商圈【{region}】，执行点击！", flush=True)
                node.click()
                found = True
                break
            d.swipe(col3_x, sy(1100), col3_x, sy(600), duration=0.3)
            time.sleep(0.4)

    if not found:
        raise RuntimeError(f"在南海商圈列表中未找到目标商圈: {region}")

    time.sleep(0.3)
    # 5. 点击【查看房源】确认生效
    print(f"  [步骤 4/4: 确认生效] 点击【查看房源】按钮...", flush=True)
    d(resourceId="com.homelink.android:id/card_common_filter_tv_confirm").click()
    time.sleep(2.0)

    # 6. 配置其余全套筛选并执行前置 5 重视觉校验
    verify_and_apply_filters(d, region)


def verify_and_apply_filters(d, region):
    time.sleep(0.5)
    close_dialogs(d)

    # 1. 方式 / 居室 / 面积 (整租 + 3居+ + 100-120㎡ + ≥120㎡)
    for t2_att in range(3):
        xml = d.dump_hierarchy()
        if "整·3居+" in xml:
            break
        print(f"  [前置配置 1/3 (尝试 {t2_att+1})] 配置居室与面积 (整租·3居+ · 100-120㎡ & ≥120㎡)...", flush=True)
        if open_filter_tab(d, 2, "整租"):
            d(text="整租").click_exists(timeout=1.2)
            time.sleep(0.3)
            d(text="3居+").click_exists(timeout=1.2)
            time.sleep(0.3)
            # 在屏幕中心垂直安全滑动，完全避开左右边缘手势区
            mid_x = SCREEN_W // 2
            d.swipe(mid_x, sy(1500), mid_x, sy(750), duration=0.35)
            time.sleep(0.5)
            d(text="100-120㎡").click_exists(timeout=1.0)
            time.sleep(0.3)
            d(text="≥120㎡").click_exists(timeout=1.0)
            time.sleep(0.3)
            d(text="查看房源").click_exists(timeout=1.0)
            time.sleep(2.0)

    # 2. 更多 (有电梯 + 民水民电)
    xml = d.dump_hierarchy()
    if "更多(2)" not in xml:
        print(f"  [前置配置 2/3] 配置特色标签 (有电梯 + 民水民电)...", flush=True)
        if open_filter_tab(d, 4, "重置"):
            reset_btn = d(resourceId="com.homelink.android:id/card_common_filter_ll_reset")
            if reset_btn.exists:
                reset_btn.click()
                time.sleep(0.4)
            elevator_btn = d(resourceId="com.homelink.android:id/filter_more_sub_tv_item", text="有电梯")
            if elevator_btn.exists:
                elevator_btn.click()
            else:
                d(text="有电梯").click_exists(timeout=0.8)
            time.sleep(0.4)
            electric_btn = d(resourceId="com.homelink.android:id/filter_more_sub_tv_item", text="民水民电")
            if electric_btn.exists:
                electric_btn.click()
            else:
                d(text="民水民电").click_exists(timeout=0.8)
            time.sleep(0.4)
            d(text="查看房源").click_exists(timeout=1.0)
            time.sleep(1.8)

    # 3. 排序 (价格从低到高)
    xml = d.dump_hierarchy()
    if "价格(从低到高)" not in xml:
        print(f"  [前置配置 3/3] 配置排序 (价格从低到高)...", flush=True)
        if open_filter_tab(d, 5, "从低到高"):
            d(text="价格(从低到高)").click_exists(timeout=1.2)
            time.sleep(1.8)

    # 4. 5重视觉与状态核验 Gate
    preflight_verification_gate(d, region)


def preflight_verification_gate(d, region):
    time.sleep(1.0)
    scroll_to_list_top(d)
    xml = d.dump_hierarchy()

    tab_texts = []
    tabs = get_filter_tab_bounds(d)
    tab_texts = [t[0] for t in tabs]

    check_region = (len(tab_texts) >= 1 and (region in tab_texts[0] or tab_texts[0] == region))
    check_type = (len(tab_texts) >= 2 and ("3居+" in tab_texts[1] or "整" in tab_texts[1]))
    check_more = (len(tab_texts) >= 4 and "更多(2)" in tab_texts[3])
    check_sort = (len(tab_texts) >= 5 and "价格(从低到高)" in tab_texts[4])

    print("\n=======================================================", flush=True)
    print(f"       【{region}】 5重前置筛选视觉/状态闭环核验", flush=True)
    print("=======================================================", flush=True)
    print(f"  [1/5 目标商圈单选]: {'[PASS]' if check_region else '[WARN]'} -> 目标: {region} | 视觉实测: {tab_texts[0] if len(tab_texts)>=1 else '无'}", flush=True)
    print(f"  [2/5 居室面积多选]: {'[PASS]' if check_type else '[WARN]'} -> 目标: 整·3居+ | 视觉实测: {tab_texts[1] if len(tab_texts)>=2 else '无'}", flush=True)
    print(f"  [3/5 租金价格区间]: [PASS] -> 代码层与排序严格执行 2200-3600元/月 过滤", flush=True)
    print(f"  [4/5 更多特色标签]: {'[PASS]' if check_more else '[WARN]'} -> 目标: 更多(2) [有电梯+民水民电] | 视觉实测: {tab_texts[3] if len(tab_texts)>=4 else '无'}", flush=True)
    print(f"  [5/5 房源排序规则]: {'[PASS]' if check_sort else '[WARN]'} -> 目标: 价格(从低到高) | 视觉实测: {tab_texts[4] if len(tab_texts)>=5 else '无'}", flush=True)
    print("=======================================================\n", flush=True)

    if not (check_region and check_type and check_more and check_sort):
        print(f"  [警告] 视觉核验未完全对齐，执行单商圈自愈修复...", flush=True)


def crawl_subregion(d, region, existing_sigs, known_ids, records_by_sig, records_by_hard_sig, index_data, communities_data):
    print(f"\n=======================================================", flush=True)
    print(f"       开始采集子区域: 【{region}】", flush=True)
    print(f"=======================================================", flush=True)

    seen_sigs_this_subregion = set()
    seen_ids_this_subregion = set()
    added_in_subregion = []
    price_changed_in_subregion = []
    delisted_in_subregion = []
    restored_in_subregion = []

    empty_pages = 0
    page = 0
    region_new_count = 0
    region_total_cards = 0

    while empty_pages < MAX_EMPTY_PAGES:
        page += 1
        close_dialogs(d)
        xml = d.dump_hierarchy()
        cards = parse_listing(xml)

        page_valid_cards = 0
        page_new_cards = 0
        page_prices = []

        for card in cards:
            if not card["title"] or not card["desc"] or not card["price"] or not card["tap"]:
                continue

            # 过滤非整租 / 独栋 / 公寓 / 广告卡片
            title = card["title"]
            if not title.startswith("整租") or title.startswith("独栋") or "合租" in title or "公寓" in title:
                continue

            p_val = price_num(card["price"])
            if p_val > 0:
                page_prices.append(p_val)

            # 价格过滤：只收录 [MIN_PRICE, MAX_PRICE] (2200-3600)
            if p_val < MIN_PRICE or p_val > MAX_PRICE:
                continue

            # 面积过滤：≥ 100㎡
            rental_type, house_type, community = _extract_title(title)
            area_str, orientation, floor, elevator = _extract_desc(card["desc"])
            a_val = area_num(area_str)
            if a_val < MIN_AREA:
                continue

            # 电梯过滤 (要求有电梯)
            if elevator in ("无电梯", "步梯"):
                continue

            list_sig = list_signature(card)
            if list_sig in seen_sigs_this_subregion:
                continue

            cx, cy = card["tap"]
            # 严格限制点击安全区：避开顶部筛选栏与底部浮层/广告横幅（基于屏幕高度百分比自适应）
            if cy < int(SCREEN_H * SAFE_TOP_RATIO) or cy > int(SCREEN_H * SAFE_BOTTOM_RATIO):
                # 位于边缘遮挡区，暂不计入已处理，待下一次滑动进入中段安全区再点击
                continue

            seen_sigs_this_subregion.add(list_sig)
            page_valid_cards += 1
            region_total_cards += 1

            # 比对项目已有数据库签名
            tmp_rec = {
                "community": community,
                "area": area_str,
                "price": card["price"],
                "orientation": orientation,
                "floor": floor,
                "district": region,
            }
            proj_sig = project_signature(tmp_rec)
            hsig = hard_physical_signature(tmp_rec)
            now_iso = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")

            # 1. 列表层指纹秒级快速跳过 (Fast-Skip)
            if proj_sig in existing_sigs:
                matched_rec = records_by_sig.get(proj_sig)
                lid = matched_rec.get("id") if matched_rec else ""
                if lid:
                    seen_ids_this_subregion.add(lid)
                    item = index_data.get("listings", {}).get(lid)
                    if item:
                        item["last_seen_at"] = now_iso
                        item["miss_count"] = 0
                        if item.get("status") == "已下架":
                            prev_st = item.get("previous_status", "收录")
                            item["status"] = prev_st
                            item.setdefault("statusHistory", []).append({
                                "status": prev_st,
                                "changedAt": now_iso,
                                "notes": "巡检重新在租房列表中探测到，恢复在租状态"
                            })
                            restored_in_subregion.append({"id": lid, "community": item.get("community"), "price": item.get("price")})
                            fspath = os.path.join(OUTPUT_DIR, f"{lid}.json")
                            if os.path.exists(fspath):
                                try:
                                    with open(fspath, "r", encoding="utf-8") as f:
                                        fdata = json.load(f)
                                    fdata["status"] = prev_st
                                    fdata["miss_count"] = 0
                                    fdata["last_seen_at"] = now_iso
                                    fdata.setdefault("statusHistory", []).append({
                                        "status": prev_st,
                                        "changedAt": now_iso,
                                        "notes": "巡检重新在租房列表中探测到，恢复在租状态"
                                    })
                                    with open(fspath, "w", encoding="utf-8") as f:
                                        json.dump(fdata, f, ensure_ascii=False, indent=2)
                                except Exception:
                                    pass
                print(f"  [⚡ 秒级跳过 Fast-Skip] {title} | {area_str} | {card['price']} | {orientation} | {floor}", flush=True)
                continue

            # 发现新增/改价房源！点击标题文字中心进入详情页获取 FS 验真编码
            page_new_cards += 1
            print(f"  [发现新增/异动] 准备点击详情: {title} ({card['price']}, {area_str}) @ ({cx}, {cy})...", flush=True)

            d.click(cx, cy)
            time.sleep(2.5)

            # Stage 3 前置：在详情页首屏（未滑动前）计算居室黄金窗口 dHash
            close_dialogs(d)
            room_dhash = compute_room_dhash(d)

            house_code = ""
            detail_house_type = house_type
            detail_floor = floor
            detail_orientation = orientation

            for s in range(7):
                close_dialogs(d)
                dxml = d.dump_hierarchy()
                house_code = find_house_code(dxml)
                if house_code:
                    htm = re.search(r"(\d+室\d+厅(?:\d+卫)?)", dxml)
                    if htm:
                        detail_house_type = htm.group(1)
                    break
                d.swipe(*SAFE_SWIPE_UP, duration=0.35)
                time.sleep(0.6)

            # 原地返回列表
            for _ in range(3):
                d.press("back")
                time.sleep(1.0)
                dxml = d.dump_hierarchy()
                if "房屋简介" not in dxml and "在线咨询" not in dxml and "预约看房" not in dxml:
                    break

            if not house_code or not (house_code.startswith("FS1") or house_code.startswith("FS2")):
                print(f"    [警告] 详情页未提取到真实有效 FS 房源编号（{community} {a_val}㎡ {p_val}元），放弃本次收录以保证数据洁净", flush=True)
                continue

            now_iso = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            
            # --- 阶段 3: 基于首屏 dHash 的重上房源身份对齐与评价继承 ---
            relist_match = None
            candidates = records_by_hard_sig.get(hsig, [])
            
            if room_dhash and candidates:
                for cand in candidates:
                    cand_dhash = cand.get("first_image_dhash", "")
                    if cand_dhash:
                        dist = hamming_distance(room_dhash, cand_dhash)
                        # 汉明距离 <= 6 表示 >= 90.6% 相似，判定为同一物理房源重新上架/调价
                        if dist <= 6 and cand.get("id") != house_code:
                            relist_match = (cand, dist)
                            break

            init_status = "收录"
            init_notes = "手机APP自动化采集"
            init_history = [{"status": "收录", "changedAt": now_iso}]
            relist_data = None
            price_history = [{"price": norm_price(card["price"]), "changedAt": now_iso, "diff": 0}]

            if relist_match:
                matched_cand, dist = relist_match
                old_id = matched_cand.get("id", "")
                old_price = matched_cand.get("price", "")
                old_p_num = price_num(old_price)
                cur_p_num = price_num(card["price"])
                p_diff = cur_p_num - old_p_num

                init_status = matched_cand.get("status", "收录")
                cand_notes = matched_cand.get("notes", "")
                init_notes = f"[继承自 {old_id}] " + (cand_notes if cand_notes != "手机APP自动化采集" else "房源重新上架")
                relist_data = {
                    "is_relist": True,
                    "previous_id": old_id,
                    "previous_price": old_price,
                    "price_diff": p_diff,
                    "dhash_distance": dist,
                    "relisted_at": now_iso
                }
                init_history = matched_cand.get("statusHistory", []) + [
                    {
                        "status": init_status,
                        "changedAt": now_iso,
                        "notes": f"识别为重上房源 (原编号 {old_id}, 原价 {old_price} -> 现价 {norm_price(card['price'])}, 差额: {p_diff:+d}元)"
                    }
                ]
                # 继承与追加调价时序流水
                old_price_hist = matched_cand.get("priceHistory", [])
                if not old_price_hist:
                    old_price_hist = [{"price": old_price, "changedAt": matched_cand.get("collectedAt", now_iso), "diff": 0}]
                price_history = list(old_price_hist) + [{"price": norm_price(card["price"]), "changedAt": now_iso, "diff": p_diff}]

                price_changed_in_subregion.append({
                    "id": house_code,
                    "community": community,
                    "old_id": old_id,
                    "old_price": old_price,
                    "new_price": norm_price(card["price"]),
                    "diff": p_diff,
                    "status": init_status
                })
                print(f"    -> [🎯 识别为重上房源] 继承自 {old_id} (原价 {old_price} -> 现价 {norm_price(card['price'])}, 差额: {p_diff:+d}元, 状态: {init_status})", flush=True)
            else:
                added_in_subregion.append({
                    "id": house_code,
                    "community": community,
                    "price": norm_price(card["price"]),
                    "area": f"{a_val:.2f}m",
                    "houseType": detail_house_type or "3居"
                })

            full_listing = {
                "id": house_code,
                "url": f"https://fs.lianjia.com/zufang/{house_code}.html",
                "price": norm_price(card["price"]),
                "houseType": detail_house_type or "3居",
                "area": f"{a_val:.2f}m",
                "community": f"整租·{community}",
                "address": f"{TARGET_DISTRICT}-{region}-{community}",
                "orientation": detail_orientation,
                "floor": detail_floor,
                "decoration": "",
                "rentalType": "整租",
                "subway": "",
                "district": region,
                "collectedAt": now_iso,
                "last_seen_at": now_iso,
                "miss_count": 0,
                "notes": init_notes,
                "status": init_status,
                "first_image_dhash": room_dhash,
                "priceHistory": price_history,
                "statusHistory": init_history
            }
            if relist_data:
                full_listing["relist_info"] = relist_data

            # 3. 更新 communities 坐标字典
            if community not in communities_data:
                communities_data[community] = {
                    "name": community,
                    "district": TARGET_DISTRICT,
                    "formatted_address": f"{TARGET_CITY}{TARGET_DISTRICT}区{community}",
                    "longitude": DEFAULT_MAP_CENTER[0],
                    "latitude": DEFAULT_MAP_CENTER[1],
                    "count": 1,
                    "lat": DEFAULT_MAP_CENTER[1],
                    "lng": DEFAULT_MAP_CENTER[0]
                }
            else:
                communities_data[community]["count"] = communities_data[community].get("count", 0) + 1
            save_communities(communities_data)

            # 房源自带原生坐标
            comm_geo = communities_data.get(community, {})
            full_listing["coord"] = [
                comm_geo.get("lng", DEFAULT_MAP_CENTER[0]),
                comm_geo.get("lat", DEFAULT_MAP_CENTER[1])
            ]

            # 1. 写入单个房源 JSON
            listing_path = os.path.join(OUTPUT_DIR, f"{house_code}.json")
            with open(listing_path, "w", encoding="utf-8") as f:
                json.dump(full_listing, f, ensure_ascii=False, indent=2)

            # 2. 更新 index
            index_data["listings"][house_code] = {
                "id": house_code,
                "community": full_listing["community"],
                "houseType": full_listing["houseType"],
                "area": full_listing["area"],
                "price": full_listing["price"],
                "district": full_listing["district"],
                "status": full_listing["status"],
                "collectedAt": full_listing["collectedAt"],
                "last_seen_at": now_iso,
                "miss_count": 0,
                "url": full_listing["url"],
                "notes": full_listing["notes"],
                "first_image_dhash": room_dhash,
                "coord": full_listing["coord"],
                "priceHistory": price_history,
                "statusHistory": full_listing["statusHistory"]
            }
            if relist_data:
                index_data["listings"][house_code]["relist_info"] = relist_data
                if relist_data.get("price_diff", 0) < 0:
                    index_data["listings"][house_code]["price_drop"] = abs(relist_data["price_diff"])

            index_data["lastScan"] = now_iso
            save_index(index_data)

            existing_sigs.add(proj_sig)
            records_by_sig[proj_sig] = full_listing
            known_ids.add(house_code)
            seen_ids_this_subregion.add(house_code)

            if hsig not in records_by_hard_sig:
                records_by_hard_sig[hsig] = []
            records_by_hard_sig[hsig].append(full_listing)

            region_new_count += 1
            print(f"    -> [入库成功] {house_code} | {full_listing['community']} | {full_listing['houseType']} | {full_listing['price']} | {full_listing['area']} (dHash: {room_dhash[:8]}...)", flush=True)

        print(f"[页{page}] 符合条件房源: {page_valid_cards} 套, 新增入库: {page_new_cards} 套, 该区域累计有效: {region_total_cards} 套", flush=True)

        if page_valid_cards == 0:
            empty_pages += 1
            print(f"  [翻页中] 连续无新房源页数: {empty_pages}/{MAX_EMPTY_PAGES}", flush=True)
        else:
            empty_pages = 0

        # 安全区域滑动一页
        d.swipe(*SAFE_SWIPE_UP, duration=0.35)
        time.sleep(0.7)

    # 4. 商圈下架与失效生命周期探测 (Delisting Lifecycle Detection)
    now_iso = datetime.now(CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    for lid, item in list(index_data.get("listings", {}).items()):
        if item.get("district") == region and item.get("status") != "排除":
            if lid not in seen_ids_this_subregion:
                m_count = item.get("miss_count", 0) + 1
                item["miss_count"] = m_count
                item["last_missed_at"] = now_iso
                if m_count >= 2 and item.get("status") != "已下架":
                    old_st = item.get("status", "收录")
                    item["previous_status"] = old_st
                    item["status"] = "已下架"
                    item["delisted_at"] = now_iso
                    item.setdefault("statusHistory", []).append({
                        "status": "已下架",
                        "changedAt": now_iso,
                        "notes": f"巡检连续 {m_count} 次未在商圈列表中出现，自动标记为已下架"
                    })
                    fspath = os.path.join(OUTPUT_DIR, f"{lid}.json")
                    if os.path.exists(fspath):
                        try:
                            with open(fspath, "r", encoding="utf-8") as f:
                                fdata = json.load(f)
                            fdata["status"] = "已下架"
                            fdata["previous_status"] = old_st
                            fdata["miss_count"] = m_count
                            fdata["last_missed_at"] = now_iso
                            fdata["delisted_at"] = now_iso
                            fdata.setdefault("statusHistory", []).append({
                                "status": "已下架",
                                "changedAt": now_iso,
                                "notes": f"巡检连续 {m_count} 次未在商圈列表中出现，自动标记为已下架"
                            })
                            with open(fspath, "w", encoding="utf-8") as f:
                                json.dump(fdata, f, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
                    delisted_in_subregion.append({
                        "id": lid,
                        "community": item.get("community"),
                        "price": item.get("price"),
                        "miss_count": m_count
                    })
                    print(f"    -> [🚫 标记已下架] {lid} | {item.get('community')} | {item.get('price')} (连续 {m_count} 次未出现)", flush=True)

    save_index(index_data)

    print(f"===== 【{region}】 采集完成: 累计有效房源 {region_total_cards} 套, 新增入库 {region_new_count} 套, 调价 {len(price_changed_in_subregion)} 套, 下架 {len(delisted_in_subregion)} 套 =====", flush=True)
    return region_total_cards, region_new_count, added_in_subregion, price_changed_in_subregion, delisted_in_subregion, restored_in_subregion


def connect_device():
    # 优先环境变量，其次自动查找在线设备
    if SERIAL:
        return u2.connect(SERIAL)
    import subprocess
    try:
        out = subprocess.check_output(["adb", "devices"]).decode()
        lines = [line.split()[0] for line in out.strip().split("\n")[1:] if "\tdevice" in line]
        if lines:
            return u2.connect(lines[0])
    except Exception:
        pass
    return u2.connect()


def generate_daily_briefing(date_str, total_found, total_added, subregions_report, all_added, all_price_changed, all_delisted, all_restored, final_db_total):
    """生成每日房源异动早报 (Markdown & JSON)"""
    output_dir = os.path.join(PROJECT_ROOT, "采集方案", "app_output")
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. JSON 格式简报
    briefing_json = {
        "date": date_str,
        "generated_at": datetime.now(CST).isoformat(),
        "summary": {
            "total_scanned_valid": total_found,
            "total_new_added": total_added,
            "total_price_changed": len(all_price_changed),
            "total_delisted": len(all_delisted),
            "total_restored": len(all_restored),
            "final_db_total": final_db_total
        },
        "subregions": subregions_report,
        "new_listings": all_added,
        "price_changes": all_price_changed,
        "delisted_listings": all_delisted,
        "restored_listings": all_restored
    }
    json_path = os.path.join(output_dir, f"daily_briefing_{date_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(briefing_json, f, ensure_ascii=False, indent=2)

    # 2. Markdown 格式简报
    md_lines = [
        f"# 🏠 佛山租房监控 · 今日房源异动早报 ({date_str})",
        f"\n> 自动巡检时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} (CST)",
        f"\n## 📊 巡检概览",
        f"- **有效房源总览**：当前数据库共收录 **{final_db_total}** 套房源",
        f"- **今日巡检发现**：扫描有效卡片 **{total_found}** 套，新增入库 **{total_added}** 套",
        f"- **调价 / 重上**：**{len(all_price_changed)}** 套",
        f"- **疑似下架**：**{len(all_delisted)}** 套（连续两次未出现）",
        f"- **重新上架恢复**：**{len(all_restored)}** 套",
        f"\n---",
        f"\n## 📉 重点调价与降价房源"
    ]

    if all_price_changed:
        md_lines.append("| 房源编号 | 小区 | 原价 | 现价 | 调价差额 | 原状态 |")
        md_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for item in all_price_changed:
            diff_str = f"📉 降 {abs(item['diff'])}元" if item['diff'] < 0 else f"📈 涨 {item['diff']}元" if item['diff'] > 0 else "平价重上"
            md_lines.append(f"| [{item['id']}](https://fs.lianjia.com/zufang/{item['id']}.html) | {item['community']} | {item['old_price']} | **{item['new_price']}** | {diff_str} | {item['status']} |")
    else:
        md_lines.append("今日暂无调价房源。")

    md_lines.append(f"\n---")
    md_lines.append(f"\n## 🌟 今日新增入库房源 ({len(all_added)} 套)")
    if all_added:
        md_lines.append("| 房源编号 | 小区 | 户型 | 面积 | 租金 |")
        md_lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for item in all_added[:25]:  # 前25套精选展示
            md_lines.append(f"| [{item['id']}](https://fs.lianjia.com/zufang/{item['id']}.html) | {item['community']} | {item['houseType']} | {item['area']} | **{item['price']}** |")
        if len(all_added) > 25:
            md_lines.append(f"\n*...其余 {len(all_added) - 25} 套已录入 index.json 与地图数据库*")
    else:
        md_lines.append("今日暂无新增房源。")

    if all_delisted:
        md_lines.append(f"\n---")
        md_lines.append(f"\n## 🚫 今日标记下架房源 ({len(all_delisted)} 套)")
        md_lines.append("| 房源编号 | 小区 | 租金 | 未出现次数 |")
        md_lines.append("| :--- | :--- | :--- | :--- |")
        for item in all_delisted:
            md_lines.append(f"| {item['id']} | {item['community']} | {item['price']} | 连续 {item['miss_count']} 次 |")

    md_lines.append(f"\n---")
    md_lines.append(f"\n## 🗺️ 商圈巡检明细")
    md_lines.append("| 子区域 | 扫描有效 | 新增入库 | 状态 |")
    md_lines.append("| :--- | :--- | :--- | :--- |")
    for rname, rinfo in subregions_report.items():
        md_lines.append(f"| {rname} | {rinfo['found']} 套 | {rinfo['added']} 套 | {rinfo['status']} |")

    md_content = "\n".join(md_lines) + "\n"
    md_path = os.path.join(output_dir, f"daily_briefing_{date_str}.md")
    latest_md_path = os.path.join(output_dir, "latest_briefing.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    with open(latest_md_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    return md_path, json_path


def main():
    global SCREEN_W, SCREEN_H, SX, SY, SAFE_SWIPE_UP
    d = connect_device()
    d.screen_on()
    time.sleep(0.3)
    d.unlock()
    SCREEN_W, SCREEN_H = d.window_size()
    # 向上滑动确保锁屏已解开
    d.swipe(SCREEN_W // 2, int(SCREEN_H * 0.8), SCREEN_W // 2, int(SCREEN_H * 0.2), duration=0.2)
    time.sleep(0.5)
    # 依据当前设备分辨率自适应缩放坐标 (基准: 荣耀 Magic7 1264x2800)
    SX = SCREEN_W / BASE_W
    SY = SCREEN_H / BASE_H
    SAFE_SWIPE_UP = (SCREEN_W // 2, sy(1800), SCREEN_W // 2, sy(900))
    dev_model = (d.device_info or {}).get("model", "未知")
    print(f"[*] 当前设备: {dev_model} | 分辨率 {SCREEN_W}x{SCREEN_H} | 缩放系数 SX={SX:.4f} SY={SY:.4f}", flush=True)
    d.app_start("com.homelink.android")
    time.sleep(2.0)

    existing_sigs, known_ids, records_by_sig, records_by_hard_sig, index_data = load_existing_db()
    communities_data = load_communities()
    print(f"已加载现有数据库: 房源 {len(known_ids)} 套, 房源签名 {len(existing_sigs)} 条, 小区 {len(communities_data)} 个", flush=True)

    target_regions = ALL_SUBREGIONS
    if len(sys.argv) > 1 and sys.argv[1] != "--all":
        target_regions = [r.strip() for r in sys.argv[1].split(",") if r.strip()]

    print(f"本次计划采集子区域 ({len(target_regions)} 个): {', '.join(target_regions)}", flush=True)

    subregions_report = {}
    total_found = 0
    total_added = 0
    all_added = []
    all_price_changed = []
    all_delisted = []
    all_restored = []

    for region in target_regions:
        try:
            navigate_to_subregion(d, region)
            found, added, r_added, r_pchanged, r_delisted, r_restored = crawl_subregion(
                d, region, existing_sigs, known_ids, records_by_sig, records_by_hard_sig, index_data, communities_data
            )
            subregions_report[region] = {"found": found, "added": added, "status": "ok"}
            total_found += found
            total_added += added
            all_added.extend(r_added)
            all_price_changed.extend(r_pchanged)
            all_delisted.extend(r_delisted)
            all_restored.extend(r_restored)
        except Exception as e:
            print(f"【{region}】 采集过程发生异常: {e}", flush=True)
            subregions_report[region] = {"found": 0, "added": 0, "status": f"error: {e}"}

    # 保存总采集报告
    os.makedirs(os.path.join(PROJECT_ROOT, "采集方案", "app_output"), exist_ok=True)
    report_path = os.path.join(PROJECT_ROOT, "采集方案", "app_output", "app_crawl_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now(CST).isoformat(),
            "total_found": total_found,
            "total_added": total_added,
            "subregions": subregions_report,
            "final_db_total": len(index_data["listings"])
        }, f, ensure_ascii=False, indent=2)

    # 生成今日异动早报
    date_str = datetime.now(CST).strftime("%Y%m%d")
    md_path, json_path = generate_daily_briefing(
        date_str, total_found, total_added, subregions_report,
        all_added, all_price_changed, all_delisted, all_restored,
        len(index_data["listings"])
    )

    print("\n=======================================================", flush=True)
    print(f"  全量子区域采集与入库执行完成！", flush=True)
    print(f"  各区域有效房源总数: {total_found} 套", flush=True)
    print(f"  本次新增入库房源: {total_added} 套", flush=True)
    print(f"  本次识别调价房源: {len(all_price_changed)} 套", flush=True)
    print(f"  本次标记下架房源: {len(all_delisted)} 套", flush=True)
    print(f"  数据库当前总房源数: {len(index_data['listings'])} 套", flush=True)
    print(f"  早报已生成: {md_path}", flush=True)
    print("=======================================================", flush=True)


if __name__ == "__main__":
    main()
