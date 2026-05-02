"""清空并重新生成 user / job / resume 及配套日志的拟真种子数据。

用法（在 Windows 仓库根目录下执行；MySQL 跑在本机 127.0.0.1，账号读 backend/.env）：

    D:/work/JobBridge/backend/.venv/Scripts/python.exe scripts/reset_and_seed_data.py        # 清+灌（交互确认）
    D:/work/JobBridge/backend/.venv/Scripts/python.exe scripts/reset_and_seed_data.py --yes  # 跳过确认
    D:/work/JobBridge/backend/.venv/Scripts/python.exe scripts/reset_and_seed_data.py --reset-only --yes
    D:/work/JobBridge/backend/.venv/Scripts/python.exe scripts/reset_and_seed_data.py --seed-only --yes

设计方案见：C:/Users/47791/.claude/plans/cozy-forging-lark.md
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pymysql
from dotenv import dotenv_values

# 固定种子，便于结果可复现
random.seed(20260502)

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / "backend" / ".env"


# ============================================================================
# 配置常量
# ============================================================================

ID_PREFIX_FACTORY = "seed_factory_"
ID_PREFIX_BROKER = "seed_broker_"
ID_PREFIX_WORKER = "seed_worker_"

NUM_FACTORY = 50
NUM_BROKER = 30
NUM_WORKER = 150
NUM_JOBS = 200
NUM_RESUMES = 120

# 核心城市（70% 岗位 + 简历首选都从这里抽，保证匹配密度）
CORE_CITIES = ["苏州市", "无锡市", "上海市", "杭州市",
               "深圳市", "东莞市", "广州市", "合肥市"]
# 其余热区城市（30% 分配）
EXTRA_HOT_CITIES = ["常州市", "南京市", "南通市", "宁波市", "嘉兴市",
                    "佛山市", "中山市", "厦门市", "泉州市"]
HOT_CITIES = CORE_CITIES + EXTRA_HOT_CITIES
COLD_CITIES = ["徐州市", "盐城市", "金华市", "惠州市", "汕头市", "福州市"]

JOB_CATS_HOT = ["电子厂", "服装厂", "食品厂", "物流仓储", "普工", "技工"]
JOB_CATS_COLD = ["餐饮", "保安", "月嫂", "其他"]

# 工种 → (薪资下限范围, 薪资上限范围, pay_type 候选)
JOB_SALARY = {
    "电子厂":   (5000, 6500, 7000, 9000, ["月薪", "时薪"]),
    "服装厂":   (4500, 6000, 6500, 8500, ["计件", "月薪"]),
    "食品厂":   (4500, 5800, 6000, 7500, ["月薪"]),
    "物流仓储": (5500, 6500, 7000, 8500, ["月薪", "时薪"]),
    "普工":     (4500, 5800, 5800, 7200, ["月薪"]),
    "技工":     (7000, 9000, 9500, 13000, ["月薪"]),
    "餐饮":     (4500, 5500, 5500, 7000, ["月薪"]),
    "保安":     (4200, 5000, 5200, 6500, ["月薪"]),
    "月嫂":     (8000, 12000, 12000, 18000, ["月薪"]),
    "其他":     (4500, 6000, 6500, 8000, ["月薪"]),
}

# 工种性别偏好
JOB_GENDER_BIAS = {
    "电子厂":   {"不限": 0.5, "女": 0.3, "男": 0.2},
    "服装厂":   {"女": 0.7, "不限": 0.3},
    "食品厂":   {"不限": 0.6, "女": 0.3, "男": 0.1},
    "物流仓储": {"男": 0.6, "不限": 0.4},
    "普工":     {"不限": 0.7, "男": 0.2, "女": 0.1},
    "技工":     {"男": 0.7, "不限": 0.3},
    "餐饮":     {"不限": 0.6, "女": 0.3, "男": 0.1},
    "保安":     {"男": 0.85, "不限": 0.15},
    "月嫂":     {"女": 1.0},
    "其他":     {"不限": 0.7, "男": 0.2, "女": 0.1},
}

SHIFT_PATTERNS = ["白班", "两班倒", "三班倒", "做六休一", "白班+小夜班"]

# 60 个常见姓
SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳")
# 男名字
MALE_NAMES = ["伟", "强", "磊", "军", "勇", "杰", "涛", "斌", "波", "辉",
              "刚", "健", "鹏", "明", "亮", "东", "建国", "建华", "志强", "志刚",
              "建军", "晓东", "晓明", "海涛", "宇航", "鑫", "凯", "浩", "超", "龙",
              "建国", "国强", "永刚", "永军", "建平", "新民", "卫东", "建中", "国华",
              "学军"]
# 女名字
FEMALE_NAMES = ["芳", "娟", "敏", "静", "丽", "强", "玲", "艳", "霞", "梅",
                "婷", "雪", "月", "燕", "桂兰", "桂英", "桂芳", "秀英", "秀兰", "秀梅",
                "玉兰", "玉梅", "凤英", "凤兰", "凤霞", "美玲", "雅楠", "晓燕", "晓梅", "晓红",
                "春兰", "春梅", "春燕", "春香", "金凤", "金花", "桂珍", "丽华", "丽娟", "晓敏"]

# 公司名生成
COMPANY_CITY_SHORT = {
    "苏州市": "苏州", "无锡市": "无锡", "常州市": "常州", "南京市": "南京", "南通市": "南通",
    "上海市": "上海", "杭州市": "杭州", "宁波市": "宁波", "嘉兴市": "嘉兴",
    "深圳市": "深圳", "东莞市": "东莞", "广州市": "广州", "佛山市": "佛山", "中山市": "中山",
    "厦门市": "厦门", "泉州市": "泉州", "合肥市": "合肥",
}
COMPANY_PARK = ["", "工业园", "经开区", "高新区", "科技园", "出口加工区"]
COMPANY_BRAND = ["睿联", "锦华", "正元", "鼎泰", "嘉誉", "宏远", "万恒", "盛达",
                 "新象", "东方", "百川", "天和", "联诚", "瑞达", "金达", "广源",
                 "源信", "迅成", "立信", "兴业", "中泰", "顺达", "永利", "汇通"]
COMPANY_INDUSTRY = ["电子", "服装", "精密", "实业", "科技", "食品", "物流", "智能",
                    "机械", "纺织", "针织", "塑胶", "包装", "五金", "光电"]

PHONE_PREFIXES = ["138", "139", "150", "152", "158", "180", "186", "188", "133", "176"]

# 各热区/核心城市常见区县（含产业聚集地，制造业园区优先）
DISTRICTS_BY_CITY = {
    "苏州市": ["工业园区", "高新区", "相城区", "吴中区", "吴江区", "昆山市", "太仓市", "张家港市"],
    "无锡市": ["新吴区", "锡山区", "惠山区", "江阴市", "宜兴市", "滨湖区"],
    "常州市": ["武进区", "新北区", "天宁区", "金坛区"],
    "南京市": ["江宁区", "浦口区", "六合区", "栖霞区"],
    "南通市": ["崇川区", "通州区", "海门区", "如东县", "启东市"],
    "上海市": ["浦东新区", "闵行区", "嘉定区", "松江区", "青浦区", "奉贤区", "宝山区", "金山区"],
    "杭州市": ["余杭区", "萧山区", "临平区", "钱塘区", "富阳区", "临安区"],
    "宁波市": ["北仑区", "鄞州区", "镇海区", "慈溪市", "余姚市", "象山县"],
    "嘉兴市": ["秀洲区", "南湖区", "嘉善县", "海宁市", "桐乡市", "平湖市"],
    "深圳市": ["宝安区", "龙华区", "龙岗区", "南山区", "光明区", "坪山区"],
    "东莞市": ["长安镇", "虎门镇", "塘厦镇", "清溪镇", "厚街镇", "凤岗镇", "大朗镇"],
    "广州市": ["番禺区", "南沙区", "增城区", "白云区", "黄埔区", "花都区"],
    "佛山市": ["顺德区", "南海区", "禅城区", "三水区", "高明区"],
    "中山市": ["小榄镇", "古镇镇", "三乡镇", "横栏镇", "南头镇"],
    "厦门市": ["集美区", "海沧区", "同安区", "翔安区", "湖里区"],
    "泉州市": ["晋江市", "石狮市", "南安市", "惠安县", "鲤城区"],
    "合肥市": ["高新区", "经开区", "新站区", "肥东县", "肥西县", "庐阳区"],
    # 冷区城市
    "徐州市": ["铜山区", "云龙区", "鼓楼区"],
    "盐城市": ["亭湖区", "盐都区", "大丰区"],
    "金华市": ["金东区", "义乌市", "永康市", "兰溪市"],
    "惠州市": ["惠城区", "惠阳区", "博罗县"],
    "汕头市": ["金平区", "龙湖区", "潮南区"],
    "福州市": ["仓山区", "闽侯县", "长乐区", "马尾区"],
}

# 街道/路 名候选池
STREET_PATTERNS = [
    "{base}路", "{base}大道", "{base}街", "{base}北路", "{base}南路",
    "{base}东路", "{base}西路", "{base}中路",
]
STREET_BASES = [
    "人民", "中山", "建设", "解放", "文化", "迎宾", "友谊", "工业", "科技",
    "兴业", "创业", "园区", "新华", "胜利", "国防", "黄河", "长江", "珠江",
    "光明", "太湖", "金沙", "玉兰", "梧桐", "锦绣", "锦江", "滨河",
    "淮海", "崇文", "翠湖", "凤凰", "金鸡", "银杏", "桂花", "樱花",
]


# ============================================================================
# 工具函数
# ============================================================================

def gen_phone() -> str:
    return random.choice(PHONE_PREFIXES) + "".join(random.choices("0123456789", k=8))


def gen_name(gender: str) -> str:
    surname = random.choice(SURNAMES)
    given = random.choice(MALE_NAMES if gender == "男" else FEMALE_NAMES)
    return surname + given


def gen_company(city: str) -> str:
    short = COMPANY_CITY_SHORT.get(city, city.replace("市", ""))
    park = random.choice(COMPANY_PARK)
    brand = random.choice(COMPANY_BRAND)
    industry = random.choice(COMPANY_INDUSTRY)
    suffix = random.choice(["有限公司", "股份有限公司"])
    return f"{short}{park}{brand}{industry}{suffix}"


def gen_district(city: str) -> str | None:
    """从城市的常见区县中随机抽一个。城市未在词典里时返回 None。"""
    pool = DISTRICTS_BY_CITY.get(city)
    if not pool:
        return None
    return random.choice(pool)


def gen_street_address(district: str | None = None) -> str:
    """街道+门牌：如 工业园区星湖街 328 号 / 长江北路 88 号 1 栋"""
    street = random.choice(STREET_PATTERNS).format(base=random.choice(STREET_BASES))
    door = random.choice([
        f"{random.randint(1, 999)} 号",
        f"{random.randint(1, 999)} 号 {random.randint(1, 12)} 栋",
        f"{random.randint(1, 999)} 号 {chr(random.randint(0x41, 0x46))} 区",
    ])
    prefix = district + " " if district else ""
    return f"{prefix}{street} {door}"


def random_dt(days_ago_min: int, days_ago_max: int) -> datetime:
    seconds = random.randint(days_ago_min * 86400, days_ago_max * 86400)
    return datetime.now() - timedelta(seconds=seconds)


def weighted_choice(weights: dict[str, float]) -> str:
    keys, vals = zip(*weights.items())
    return random.choices(keys, weights=vals, k=1)[0]


def status_dist(active: float, blocked: float, deleted: float) -> str:
    return weighted_choice({"active": active, "blocked": blocked, "deleted": deleted})


def audit_dist(passed: float = 0.85, pending: float = 0.08, rejected: float = 0.07) -> str:
    return weighted_choice({"passed": passed, "pending": pending, "rejected": rejected})


# ============================================================================
# 数据库连接
# ============================================================================

def db_connect() -> pymysql.connections.Connection:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"未找到 {ENV_PATH}")
    env = dotenv_values(ENV_PATH)
    conn = pymysql.connect(
        host=env.get("DB_HOST", "127.0.0.1"),
        port=int(env.get("DB_PORT", 3306)),
        user=env.get("DB_USER", "jobbridge"),
        password=env.get("DB_PASSWORD", "jobbridge"),
        database=env.get("DB_NAME", "jobbridge"),
        charset="utf8mb4",
        autocommit=False,
    )
    return conn


# ============================================================================
# Reset
# ============================================================================

TABLES_TO_REPORT = [
    "user", "job", "resume",
    "conversation_log", "wecom_inbound_event", "audit_log", "event_log",
    "admin_user",
    "dict_city", "dict_job_category", "dict_sensitive_word", "system_config",
]


def report_counts(conn: pymysql.connections.Connection, label: str) -> None:
    print(f"\n[{label}] 各表行数：")
    with conn.cursor() as cur:
        for t in TABLES_TO_REPORT:
            cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            n = cur.fetchone()[0]
            print(f"  {t:25s} {n}")


def reset_data(conn: pymysql.connections.Connection) -> None:
    print("\n[+] 清空业务数据（保留 admin_user / dict_* / system_config）")
    with conn.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in ("event_log", "audit_log", "conversation_log", "wecom_inbound_event"):
            cur.execute(f"TRUNCATE TABLE `{t}`")
        for t in ("job", "resume"):
            cur.execute(f"DELETE FROM `{t}`")
            cur.execute(f"ALTER TABLE `{t}` AUTO_INCREMENT=1")
        cur.execute("DELETE FROM `user`")
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
    conn.commit()
    print("  done.")
    seed_mock_users(conn)


def seed_mock_users(conn: pymysql.connections.Connection) -> None:
    """恢复 mock-testbed 沙箱预置用户（wm_mock_*）。

    与 mock-testbed/sql/seed_mock_users.sql 内容保持一致；放在这里是因为
    本脚本 reset 会清掉整张 user 表，沙箱前端依赖这几个固定身份。
    """
    print("[+] 重新灌入 mock-testbed 预置用户（wm_mock_*）")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO `user`
              (external_userid, role, display_name, company, contact_person, phone,
               can_search_jobs, can_search_workers, status)
            VALUES
              ('wm_mock_worker_001',  'worker',  '张工',       NULL,               '张工',   '13800000001', 1, 0, 'active'),
              ('wm_mock_worker_002',  'worker',  '李师傅',     NULL,               '李师傅', '13800000002', 1, 0, 'active'),
              ('wm_mock_factory_001', 'factory', '华东电子厂', '华东电子有限公司', '王经理', '13900000001', 0, 1, 'active'),
              ('wm_mock_broker_001',  'broker',  '速聘中介',   '速聘人力资源',     '赵中介', '13700000001', 0, 1, 'active')
            ON DUPLICATE KEY UPDATE
              role=VALUES(role), display_name=VALUES(display_name), company=VALUES(company),
              contact_person=VALUES(contact_person), phone=VALUES(phone),
              can_search_jobs=VALUES(can_search_jobs), can_search_workers=VALUES(can_search_workers),
              status=VALUES(status)
        """)
    conn.commit()
    print("  inserted 4 wm_mock_* users")


# ============================================================================
# Seed: users
# ============================================================================

def seed_users(conn: pymysql.connections.Connection) -> dict[str, list[dict[str, Any]]]:
    """生成用户并插入 user 表，返回 {role: [user_dict, ...]}（含 external_userid/gender/age 等）。"""
    print(f"\n[+] 生成用户：factory={NUM_FACTORY} broker={NUM_BROKER} worker={NUM_WORKER}")

    factories: list[dict] = []
    brokers: list[dict] = []
    workers: list[dict] = []

    # 厂家：性别比例无所谓（display_name 是联系人），但要绑公司/城市/地址
    for i in range(1, NUM_FACTORY + 1):
        gender = random.choice(["男", "女"])
        name = gen_name(gender)
        city = random.choice(list(COMPANY_CITY_SHORT.keys()))
        factory_district = gen_district(city)
        factory_addr = f"{city}{factory_district or ''}{gen_street_address()}"
        factories.append({
            "external_userid": f"{ID_PREFIX_FACTORY}{i:03d}",
            "role": "factory",
            "display_name": name,
            "company": gen_company(city),
            "address": factory_addr,
            "contact_person": name,
            "phone": gen_phone(),
            "can_search_jobs": 0,
            "can_search_workers": 1,
            "status": status_dist(0.95, 0.03, 0.02),
            "city": city,
            "district": factory_district,
        })

    for i in range(1, NUM_BROKER + 1):
        gender = random.choice(["男", "女"])
        name = gen_name(gender)
        # 30% 中介挂靠人力资源公司，有公司+地址；其余独立中介无 company/address
        if random.random() < 0.30:
            broker_city = random.choice(list(COMPANY_CITY_SHORT.keys()))
            broker_district = gen_district(broker_city)
            broker_company = f"{COMPANY_CITY_SHORT[broker_city]}{random.choice(['锦程', '汇通', '众城', '金桥', '人和'])}人力资源服务有限公司"
            broker_addr = f"{broker_city}{broker_district or ''}{gen_street_address()}"
        else:
            broker_company = None
            broker_addr = None
        brokers.append({
            "external_userid": f"{ID_PREFIX_BROKER}{i:03d}",
            "role": "broker",
            "display_name": name,
            "company": broker_company,
            "address": broker_addr,
            "contact_person": name,
            "phone": gen_phone(),
            "can_search_jobs": 1,
            "can_search_workers": 1,
            "status": status_dist(0.90, 0.05, 0.05),
            "city": None,
        })

    for i in range(1, NUM_WORKER + 1):
        gender = random.choices(["男", "女"], weights=[0.55, 0.45])[0]
        name = gen_name(gender)
        # 工人侧不暴露公司/contact_person/address
        workers.append({
            "external_userid": f"{ID_PREFIX_WORKER}{i:03d}",
            "role": "worker",
            "display_name": name,
            "company": None,
            "address": None,
            "contact_person": None,
            "phone": gen_phone(),
            "can_search_jobs": 1,
            "can_search_workers": 0,
            "status": status_dist(0.92, 0.05, 0.03),
            "gender": gender,
            "age": random.randint(18, 48),
        })

    rows = []
    for u in factories + brokers + workers:
        registered_at = random_dt(0, 90)
        # 90% 在过去 7 天活跃 / 7% 7-30 天 / 3% 30 天以上
        bucket = random.random()
        if bucket < 0.90:
            last_active = random_dt(0, 7)
        elif bucket < 0.97:
            last_active = random_dt(7, 30)
        else:
            last_active = random_dt(30, 90)
        # last_active 不能早于 registered_at
        if last_active < registered_at:
            last_active = registered_at
        blocked_reason = None
        if u["status"] == "blocked":
            blocked_reason = random.choice([
                "连续触发敏感词", "高频违规", "客诉确认违规", "上传虚假信息",
            ])
        rows.append((
            u["external_userid"], u["role"], u["display_name"], u["company"],
            u["address"], u["contact_person"], u["phone"],
            u["can_search_jobs"], u["can_search_workers"],
            u["status"], blocked_reason,
            registered_at, last_active,
            None,  # extra
        ))

    sql = """
    INSERT INTO `user`
      (external_userid, role, display_name, company, address,
       contact_person, phone,
       can_search_jobs, can_search_workers, status, blocked_reason,
       registered_at, last_active_at, extra)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    print(f"  inserted user rows: {len(rows)}")
    return {"factory": factories, "broker": brokers, "worker": workers}


# ============================================================================
# Seed: jobs
# ============================================================================

def gen_job_raw_text(city: str, cat: str, headcount: int, salary_lo: int, salary_hi: int,
                     pay_type: str, gender: str, age_min: int, age_max: int,
                     shift: str, meal: int, housing: int) -> str:
    parts = [f"{city}{cat}招"]
    if cat == "技工":
        parts.append(f"焊工/电工{headcount}人")
    elif cat == "电子厂":
        parts.append(f"普工{headcount}人")
    elif cat == "服装厂":
        parts.append(f"缝纫工{headcount}人")
    elif cat == "物流仓储":
        parts.append(f"分拣员{headcount}人")
    elif cat == "餐饮":
        parts.append(f"服务员{headcount}人")
    elif cat == "保安":
        parts.append(f"保安{headcount}人")
    elif cat == "月嫂":
        parts.append(f"月嫂{headcount}人")
    elif cat == "食品厂":
        parts.append(f"包装工{headcount}人")
    else:
        parts.append(f"{headcount}人")
    parts.append(f"，{salary_lo}-{salary_hi}{pay_type}")
    fuli = []
    if meal:
        fuli.append("包吃")
    if housing:
        fuli.append("包住")
    if fuli:
        parts.append("".join(fuli))
    parts.append(f"，{age_min}-{age_max}岁")
    if gender != "不限":
        parts.append(gender)
    parts.append(f"，{shift}")
    return "".join(parts)


def seed_jobs(conn: pymysql.connections.Connection,
              users: dict[str, list[dict]]) -> list[int]:
    print(f"\n[+] 生成岗位 {NUM_JOBS} 条")
    # 只用 active/blocked 厂家+中介作 owner（deleted 的不发新岗位）
    owners = [u for u in users["factory"] + users["broker"]
              if u["status"] != "deleted"]
    random.shuffle(owners)

    rows = []
    for i in range(NUM_JOBS):
        # 90% 热区岗位 / 10% 冷区；热区岗位中 70% 落在 8 个核心城市
        is_hot = random.random() < 0.90
        if is_hot:
            city = random.choice(CORE_CITIES if random.random() < 0.70 else EXTRA_HOT_CITIES)
            cat = random.choice(JOB_CATS_HOT)
        else:
            city = random.choice(COLD_CITIES)
            cat = random.choice(JOB_CATS_COLD)

        owner = owners[i % len(owners)]
        # 厂家更倾向用自家城市作岗位地
        same_city_as_owner = False
        if owner["role"] == "factory" and owner.get("city") and random.random() < 0.7:
            city = owner["city"]
            same_city_as_owner = True

        # 区县 + 详细地址
        # 厂家在自家城市发岗位时，70% 沿用厂家区县，30% 同城另一个区县
        if same_city_as_owner and owner.get("district") and random.random() < 0.7:
            district = owner["district"]
        else:
            district = gen_district(city)
        job_address = f"{city}{district or ''}{gen_street_address()}"

        sl_lo_min, sl_lo_max, sh_lo, sh_hi, pay_types = JOB_SALARY[cat]
        salary_floor = random.randint(sl_lo_min, sl_lo_max) // 100 * 100
        salary_ceiling = random.randint(sh_lo, sh_hi) // 100 * 100
        if salary_ceiling <= salary_floor:
            salary_ceiling = salary_floor + 500
        pay_type = random.choice(pay_types)

        gender = weighted_choice(JOB_GENDER_BIAS[cat])
        age_min = random.choice([18, 18, 18, 20, 22])
        # 制造业大多数岗位实际愿意收到 50 岁，少数到 55
        age_max = random.choice([45, 48, 50, 50, 55])
        is_long_term = 1 if random.random() < 0.90 else 0
        shift = random.choice(SHIFT_PATTERNS)
        if cat in ("技工", "餐饮", "保安", "月嫂"):
            provide_meal = 1 if random.random() < 0.7 else 0
            provide_housing = 1 if random.random() < 0.4 else 0
        else:
            provide_meal = 1 if random.random() < 0.85 else 0
            provide_housing = 1 if random.random() < 0.80 else 0
        headcount = max(1, int(random.normalvariate(20, 12)))
        headcount = min(headcount, 100)

        # 时间分布：80% created 在过去 25 天内（保证 expires_at 仍在未来），
        # 20% 在 25-60 天前（含已过期的边缘案例）
        if random.random() < 0.80:
            created_at = random_dt(0, 25)
        else:
            created_at = random_dt(25, 60)
        expires_at = created_at + timedelta(days=30)

        # 审核状态分布
        audit_status = audit_dist(0.85, 0.08, 0.07)
        audit_reason = None
        audited_by = None
        audited_at = None
        if audit_status == "passed":
            audited_by = random.choice(["system", "system", "system", "admin001"])
            audited_at = created_at + timedelta(minutes=random.randint(1, 600))
        elif audit_status == "rejected":
            audit_reason = random.choice([
                "薪资描述不清晰，缺少每日工时",
                "工作地址表述含糊",
                "与实际工作内容不符",
                "联系方式格式不正确",
                "包含夸大宣传词汇",
            ])
            audited_by = random.choice(["system", "system", "admin001"])
            audited_at = created_at + timedelta(minutes=random.randint(1, 600))
        # pending: 全 None

        # 下架原因（仅对 passed 抽样 5+3+2%）
        delist_reason = None
        if audit_status == "passed":
            r = random.random()
            if r < 0.05:
                delist_reason = "filled"
            elif r < 0.08:
                delist_reason = "manual_delist"
            elif r < 0.10 and expires_at < datetime.now():
                delist_reason = "expired"

        raw_text = gen_job_raw_text(city, cat, headcount, salary_floor, salary_ceiling,
                                    pay_type, gender, age_min, age_max, shift,
                                    provide_meal, provide_housing)
        # raw_text 里带上工作地址（真实场景厂家招工帖必带），更像生产数据
        raw_text = f"{raw_text}，地址：{job_address}"
        description = raw_text  # 简化：用 raw_text 作清洗后描述

        rows.append((
            owner["external_userid"], city, cat, salary_floor, pay_type, headcount,
            gender, age_min, age_max, is_long_term,
            district, job_address,
            salary_ceiling, provide_meal, provide_housing, shift,
            raw_text, description,
            audit_status, audit_reason, audited_by, audited_at,
            created_at, expires_at, delist_reason,
        ))

    sql = """
    INSERT INTO `job`
      (owner_userid, city, job_category, salary_floor_monthly, pay_type, headcount,
       gender_required, age_min, age_max, is_long_term,
       district, address,
       salary_ceiling_monthly, provide_meal, provide_housing, shift_pattern,
       raw_text, description,
       audit_status, audit_reason, audited_by, audited_at,
       created_at, expires_at, delist_reason)
    VALUES (%s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        cur.execute("SELECT id FROM `job` ORDER BY id")
        job_ids = [r[0] for r in cur.fetchall()]
    conn.commit()
    print(f"  inserted job rows: {len(rows)}")
    return job_ids


# ============================================================================
# Seed: resumes
# ============================================================================

def seed_resumes(conn: pymysql.connections.Connection,
                 users: dict[str, list[dict]]) -> list[int]:
    print(f"\n[+] 生成简历 {NUM_RESUMES} 条")
    workers = [w for w in users["worker"] if w["status"] != "deleted"]
    random.shuffle(workers)

    # 100 个工人各 1 条；20 个工人各 2 条；其余无简历
    primary_workers = workers[:100]
    double_workers = workers[100:120]
    pairs: list[tuple[dict, str]] = [(w, "primary") for w in primary_workers]
    pairs += [(w, "primary") for w in double_workers]
    pairs += [(w, "secondary") for w in double_workers]
    random.shuffle(pairs)
    pairs = pairs[:NUM_RESUMES]

    rows = []
    for idx, (worker, slot) in enumerate(pairs):
        gender = worker["gender"]
        age = worker["age"]
        # 5 条故意"不可匹配"边缘
        is_edge = idx < 5

        if is_edge:
            cities = random.sample(COLD_CITIES, k=random.choice([1, 2]))
            cats = random.sample(JOB_CATS_COLD, k=1)
            salary_expect = random.choice([9000, 10000, 12000, 15000])
            age = max(age, 50)  # 高龄
        else:
            # 真实场景里，工人经常写"苏州/无锡都行"，multi-city 占大头
            n_cities = 1 if random.random() < 0.40 else random.randint(2, 3)
            # 简历首选 city 也偏向 core，与岗位密度匹配
            if random.random() < 0.75:
                cities = random.sample(CORE_CITIES, k=min(n_cities, len(CORE_CITIES)))
            else:
                cities = random.sample(HOT_CITIES, k=n_cities)
            n_cats = 1 if random.random() < 0.60 else 2
            # 男性更偏物流/技工/普工，女性更偏服装/电子/食品（仅 hot cat，保证有岗可匹配）
            if gender == "男":
                cat_pool = ["电子厂", "物流仓储", "普工", "技工", "食品厂"]
            else:
                cat_pool = ["服装厂", "电子厂", "食品厂", "普工"]
            cats = random.sample(cat_pool, k=n_cats)
            # 期望薪资刻意低于岗位 floor 中位（5500-5750），保证多数能匹配
            salary_expect = random.choice([4000, 4500, 4500, 4500, 5000, 5000, 5500])

        accept_long = 1
        accept_short = 1 if random.random() < 0.30 else 0

        # 副简历（同人第 2 条）：故意切换工种方向，但仍限定在 hot cat
        if slot == "secondary":
            other_cats = [c for c in JOB_CATS_HOT if c not in cats]
            cats = random.sample(other_cats, k=1)

        raw_text = (
            f"{cities[0]}找{cats[0]}工作，{salary_expect}以上"
            f"{'，能倒班' if random.random() < 0.5 else ''}"
            f"{'，包吃住' if random.random() < 0.5 else ''}"
        )
        description = raw_text

        created_at = random_dt(0, 60)
        expires_at = created_at + timedelta(days=30)

        audit_status = audit_dist(0.85, 0.08, 0.07)
        audit_reason = None
        audited_by = None
        audited_at = None
        if audit_status == "passed":
            audited_by = random.choice(["system", "system", "admin001"])
            audited_at = created_at + timedelta(minutes=random.randint(1, 600))
        elif audit_status == "rejected":
            audit_reason = random.choice([
                "疑似重复提交", "信息不完整缺少必要字段",
                "联系方式格式不正确", "包含敏感词",
            ])
            audited_by = random.choice(["system", "admin001"])
            audited_at = created_at + timedelta(minutes=random.randint(1, 600))

        rows.append((
            worker["external_userid"],
            json.dumps(cities, ensure_ascii=False),
            json.dumps(cats, ensure_ascii=False),
            salary_expect, gender, age,
            accept_long, accept_short,
            raw_text, description,
            audit_status, audit_reason, audited_by, audited_at,
            created_at, expires_at,
        ))

    sql = """
    INSERT INTO `resume`
      (owner_userid, expected_cities, expected_job_categories,
       salary_expect_floor_monthly, gender, age,
       accept_long_term, accept_short_term,
       raw_text, description,
       audit_status, audit_reason, audited_by, audited_at,
       created_at, expires_at)
    VALUES (%s, %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
        cur.execute("SELECT id FROM `resume` ORDER BY id")
        resume_ids = [r[0] for r in cur.fetchall()]
    conn.commit()
    print(f"  inserted resume rows: {len(rows)}")
    return resume_ids


# ============================================================================
# Seed: logs (conversation_log + wecom_inbound_event + audit_log + event_log)
# ============================================================================

INTENT_TEMPLATES = {
    "chitchat":      [("你好", "您好，欢迎使用 JobBridge，请问需要找工作还是招人？")],
    "search_job":    [("{city}{cat}", "为您找到 {n} 个岗位，第一条：{city}{cat}招{n2}人..."),
                      ("{city}找{cat}", "为您找到 {n} 个岗位，第一条：..."),
                      ("{city}{cat}{salary}以上", "为您找到 {n} 个岗位...")],
    "search_worker": [("找{cat}工人", "为您找到 {n} 位求职者...")],
    "upload_job":    [("{city}{cat}招{n}人，{salary}-{salary2}{pay}",
                      "您的岗位信息已入库，正在审核...")],
    "upload_resume": [("{city}找{cat}工作，{salary}以上", "简历已提交，正在审核...")],
    "command":       [("/我的状态", "账号状态：正常，发布岗位 X 条，活跃中..."),
                      ("/找岗位", "已切换到找岗位模式..."),
                      ("/招满了", "已标记岗位为招满..."),
                      ("/续期 30", "岗位续期 30 天成功...")],
    "follow_up":     [("工资高一点的", "已调整条件，为您重新推荐 {n} 条...")],
    "show_more":     [("更多", "为您继续推荐 {n} 条...")],
}


def seed_logs(conn: pymysql.connections.Connection,
              users: dict[str, list[dict]],
              job_ids: list[int],
              resume_ids: list[int]) -> None:
    print("\n[+] 生成对话/事件/审计/小程序点击日志")

    # 选前 30 个 active 用户作为"有对话历史的人"
    active_users = [u for u in users["worker"] + users["broker"] + users["factory"]
                    if u["status"] == "active"]
    chat_users = active_users[:30]

    conv_rows = []
    inbound_rows = []
    inbound_msg_id_seen: set[str] = set()

    for u in chat_users:
        n_msgs = random.randint(5, 15)
        last_t = random_dt(0, 30)
        for _ in range(n_msgs):
            # 每条间隔 5 分钟到 6 小时
            last_t = last_t - timedelta(seconds=random.randint(300, 21600))
            # 按用户角色挑意图
            if u["role"] == "worker":
                intent = random.choices(
                    ["search_job", "follow_up", "show_more", "chitchat", "upload_resume", "command"],
                    weights=[0.45, 0.10, 0.10, 0.10, 0.15, 0.10],
                )[0]
            elif u["role"] == "factory":
                intent = random.choices(
                    ["upload_job", "command", "chitchat", "search_worker"],
                    weights=[0.40, 0.30, 0.10, 0.20],
                )[0]
            else:  # broker
                intent = random.choices(
                    ["search_job", "search_worker", "command", "follow_up", "chitchat"],
                    weights=[0.30, 0.30, 0.20, 0.10, 0.10],
                )[0]
            in_tpl, out_tpl = random.choice(INTENT_TEMPLATES[intent])
            ctx = {
                "city": random.choice(HOT_CITIES),
                "cat": random.choice(JOB_CATS_HOT),
                "salary": random.choice([4500, 5000, 5500, 6000]),
                "salary2": random.choice([7000, 7500, 8000]),
                "pay": "月薪",
                "n": random.randint(2, 8),
                "n2": random.randint(10, 30),
            }
            try:
                in_text = in_tpl.format(**ctx)
            except KeyError:
                in_text = in_tpl
            try:
                out_text = out_tpl.format(**ctx)
            except KeyError:
                out_text = out_tpl

            in_msg_id = f"seed_msg_{u['external_userid']}_{int(last_t.timestamp())}_{random.randint(1000, 9999)}"
            if in_msg_id in inbound_msg_id_seen:
                continue
            inbound_msg_id_seen.add(in_msg_id)

            expires = last_t + timedelta(days=30)
            # in
            conv_rows.append((u["external_userid"], "in", "text", in_text,
                              in_msg_id, intent, last_t, expires))
            # out（紧随 in，加 1-3 秒）
            out_t = last_t + timedelta(seconds=random.randint(1, 3))
            conv_rows.append((u["external_userid"], "out", "text", out_text,
                              None, intent, out_t, out_t + timedelta(days=30)))

            # 入站事件：状态分布
            status = random.choices(
                ["done", "received", "processing", "failed", "dead_letter"],
                weights=[0.90, 0.03, 0.02, 0.03, 0.02],
            )[0]
            retry = 0
            err = None
            ws = None
            wf = None
            if status == "done":
                ws = last_t + timedelta(milliseconds=random.randint(50, 500))
                wf = ws + timedelta(milliseconds=random.randint(200, 3000))
            elif status == "processing":
                ws = last_t + timedelta(milliseconds=random.randint(50, 500))
            elif status == "failed":
                ws = last_t
                wf = last_t + timedelta(seconds=random.randint(1, 30))
                err = "TimeoutError: LLM request timeout"
                retry = 1
            elif status == "dead_letter":
                ws = last_t
                wf = last_t + timedelta(seconds=random.randint(1, 30))
                err = "RuntimeError: unknown error after 3 retries"
                retry = 3
            inbound_rows.append((
                in_msg_id, u["external_userid"], "text", None, in_text[:500],
                status, retry, ws, wf, err, last_t,
            ))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO `conversation_log`
              (userid, direction, msg_type, content,
               wecom_msg_id, intent, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            conv_rows,
        )
        cur.executemany(
            """
            INSERT INTO `wecom_inbound_event`
              (msg_id, from_userid, msg_type, media_id, content_brief,
               status, retry_count, worker_started_at, worker_finished_at,
               error_message, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            inbound_rows,
        )
    conn.commit()
    print(f"  inserted conversation_log rows: {len(conv_rows)}")
    print(f"  inserted wecom_inbound_event rows: {len(inbound_rows)}")

    # audit_log: 给 80 个 job/resume 补审计动作
    audit_rows = []
    sample_jobs = random.sample(job_ids, k=min(50, len(job_ids)))
    sample_resumes = random.sample(resume_ids, k=min(30, len(resume_ids)))
    for jid in sample_jobs:
        action = random.choices(
            ["auto_pass", "auto_reject", "manual_pass", "manual_reject"],
            weights=[0.65, 0.10, 0.15, 0.10],
        )[0]
        operator = "system" if action.startswith("auto_") else "admin001"
        reason = None
        if action.endswith("_reject"):
            reason = random.choice(["薪资描述不清晰", "敏感词命中", "信息不完整"])
        audit_rows.append((
            "job", str(jid), action, reason, operator, None, random_dt(0, 60),
        ))
    for rid in sample_resumes:
        action = random.choices(
            ["auto_pass", "auto_reject", "manual_pass", "manual_reject"],
            weights=[0.70, 0.10, 0.10, 0.10],
        )[0]
        operator = "system" if action.startswith("auto_") else "admin001"
        reason = None
        if action.endswith("_reject"):
            reason = random.choice(["疑似重复提交", "信息不完整"])
        audit_rows.append((
            "resume", str(rid), action, reason, operator, None, random_dt(0, 60),
        ))

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO `audit_log`
              (target_type, target_id, action, reason, operator, snapshot, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            audit_rows,
        )
    conn.commit()
    print(f"  inserted audit_log rows: {len(audit_rows)}")

    # event_log: 50 条 miniprogram_click（指向已存在的 job/resume id）
    event_rows = []
    worker_userids = [w["external_userid"] for w in users["worker"] if w["status"] == "active"]
    for _ in range(50):
        target_type = random.choice(["job", "resume"])
        tid = random.choice(job_ids if target_type == "job" else resume_ids)
        occ = random_dt(0, 30)
        event_rows.append((
            "miniprogram_click", random.choice(worker_userids), target_type, tid, occ,
            json.dumps({"src": "share_card"}, ensure_ascii=False),
        ))
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO `event_log`
              (event_type, userid, target_type, target_id, occurred_at, extra)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            event_rows,
        )
    conn.commit()
    print(f"  inserted event_log rows: {len(event_rows)}")


# ============================================================================
# Verify
# ============================================================================

def backfill_address(conn: pymysql.connections.Connection) -> None:
    """给已存在的 user / job 补 address 与 district（仅当字段为 NULL 时）。

    用途：phase7_002 迁移加了 address 字段后，旧数据需要回填。
    安全：只 UPDATE 当前 NULL 的行；多次执行幂等。
    """
    print("\n[+] 回填现有数据的 address / district 字段")
    with conn.cursor() as cur:
        # ---- factory：补 address（基于 company 名里的城市猜城市，不行就随机）----
        cur.execute(
            "SELECT external_userid, company FROM `user` "
            "WHERE role='factory' AND address IS NULL"
        )
        factory_rows = cur.fetchall()
        updates_user = []
        for uid, company in factory_rows:
            # 从公司名前缀猜城市
            city = None
            if company:
                for full, short in COMPANY_CITY_SHORT.items():
                    if company.startswith(short):
                        city = full
                        break
            if not city:
                city = random.choice(list(COMPANY_CITY_SHORT.keys()))
            district = gen_district(city)
            address = f"{city}{district or ''}{gen_street_address()}"
            updates_user.append((address, uid))
        cur.executemany("UPDATE `user` SET address=%s WHERE external_userid=%s", updates_user)
        print(f"  factory user.address 回填：{len(updates_user)}")

        # ---- broker：30% 也补 address（独立中介保持 NULL，与 seed 策略一致）----
        cur.execute(
            "SELECT external_userid, company FROM `user` "
            "WHERE role='broker' AND address IS NULL"
        )
        broker_rows = cur.fetchall()
        updates_broker = []
        for uid, company in broker_rows:
            if company is not None:
                # 已有公司名 → 必须有地址
                city = random.choice(list(COMPANY_CITY_SHORT.keys()))
                district = gen_district(city)
                updates_broker.append((f"{city}{district or ''}{gen_street_address()}", uid))
            elif random.random() < 0.30:
                city = random.choice(list(COMPANY_CITY_SHORT.keys()))
                district = gen_district(city)
                updates_broker.append((f"{city}{district or ''}{gen_street_address()}", uid))
        cur.executemany("UPDATE `user` SET address=%s WHERE external_userid=%s", updates_broker)
        print(f"  broker user.address 回填：{len(updates_broker)}")

        # ---- job：补 district + address ----
        cur.execute(
            "SELECT id, city, raw_text FROM `job` WHERE address IS NULL"
        )
        job_rows = cur.fetchall()
        updates_job = []
        for jid, city, raw_text in job_rows:
            district = gen_district(city)
            address = f"{city}{district or ''}{gen_street_address()}"
            # raw_text 后缀加上地址（如果还没带）
            if raw_text and "地址：" not in raw_text:
                new_raw = f"{raw_text}，地址：{address}"
            else:
                new_raw = raw_text
            updates_job.append((district, address, new_raw, new_raw, jid))
        cur.executemany(
            "UPDATE `job` SET district=%s, address=%s, raw_text=%s, description=%s WHERE id=%s",
            updates_job,
        )
        print(f"  job district/address/raw_text 回填：{len(updates_job)}")
    conn.commit()


def verify(conn: pymysql.connections.Connection) -> None:
    print("\n[+] Sanity check")
    with conn.cursor() as cur:
        # active worker 中有简历的占比
        cur.execute("""
            SELECT COUNT(DISTINCT u.external_userid)
            FROM `user` u
            JOIN `resume` r ON r.owner_userid = u.external_userid
            WHERE u.role='worker' AND u.status='active'
              AND r.audit_status='passed' AND r.deleted_at IS NULL
        """)
        n_with_resume = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM `user` WHERE role='worker' AND status='active'")
        n_active_worker = cur.fetchone()[0]
        print(f"  active workers: {n_active_worker}, with passed resume: {n_with_resume}")

        # 随机 30 条简历 → hard_filter 命中数
        cur.execute("""
            SELECT id, expected_cities, expected_job_categories,
                   salary_expect_floor_monthly, gender, age
            FROM `resume`
            WHERE audit_status='passed' AND deleted_at IS NULL
            ORDER BY RAND() LIMIT 30
        """)
        samples = cur.fetchall()
        print(f"  hard-filter sanity (30 random passed resumes, 仅打印前 5)：")
        all_hits = []
        printed = 0
        for rid, ecs, ecats, sal, gender, age in samples:
            ecs_l = json.loads(ecs) if isinstance(ecs, str) else ecs
            ecats_l = json.loads(ecats) if isinstance(ecats, str) else ecats
            placeholders_city = ",".join(["%s"] * len(ecs_l))
            placeholders_cat = ",".join(["%s"] * len(ecats_l))
            sql = f"""
                SELECT COUNT(*)
                FROM `job`
                WHERE audit_status='passed' AND deleted_at IS NULL
                  AND delist_reason IS NULL
                  AND expires_at > NOW()
                  AND city IN ({placeholders_city})
                  AND job_category IN ({placeholders_cat})
                  AND salary_floor_monthly >= %s
                  AND gender_required IN (%s, '不限')
                  AND (age_min IS NULL OR age_min <= %s)
                  AND (age_max IS NULL OR age_max >= %s)
            """
            params = list(ecs_l) + list(ecats_l) + [sal, gender, age, age]
            cur.execute(sql, params)
            hit = cur.fetchone()[0]
            all_hits.append(hit)
            if printed < 5:
                print(f"    resume#{rid} ({ecs_l[0]}/{ecats_l[0]}/{sal}/{gender}/{age}岁) → {hit} 个匹配岗位")
                printed += 1
        n_ge3 = sum(1 for h in all_hits if h >= 3)
        n_ge1 = sum(1 for h in all_hits if h >= 1)
        n_zero = sum(1 for h in all_hits if h == 0)
        sorted_hits = sorted(all_hits)
        median = sorted_hits[len(sorted_hits) // 2]
        mean = sum(all_hits) / len(all_hits)
        print(f"  整体：{n_ge3}/30 ≥3 岗位，{n_ge1}/30 ≥1，{n_zero}/30 = 0；"
              f"mean={mean:.1f} median={median} max={max(all_hits)}")

        # 脏记录检查：passed 但 audited_at IS NULL
        cur.execute("SELECT COUNT(*) FROM `job` WHERE audit_status='passed' AND audited_at IS NULL")
        dirty_job = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM `resume` WHERE audit_status='passed' AND audited_at IS NULL")
        dirty_resume = cur.fetchone()[0]
        print(f"  dirty (passed but no audited_at): job={dirty_job} resume={dirty_resume}")


# ============================================================================
# Main
# ============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="清空并重新灌入拟真种子数据")
    parser.add_argument("--reset-only", action="store_true", help="只清空，不灌新数据")
    parser.add_argument("--seed-only", action="store_true", help="只灌新数据，不清空")
    parser.add_argument("--backfill-address", action="store_true",
                        help="只给现有 user/job 回填 address/district 字段（幂等，不动其他数据）")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args()

    if args.reset_only and args.seed_only:
        print("[!] --reset-only 和 --seed-only 互斥", file=sys.stderr)
        return 2

    conn = db_connect()
    try:
        if args.backfill_address:
            backfill_address(conn)
            return 0
        report_counts(conn, "before")
        if not args.seed_only:
            if not args.yes:
                ans = input("\n以上数据将被清空（admin_user / dict_* / system_config 保留）。确认输入 yes 继续：").strip()
                if ans.lower() != "yes":
                    print("[x] 用户取消")
                    return 1
            reset_data(conn)
        if not args.reset_only:
            users = seed_users(conn)
            job_ids = seed_jobs(conn, users)
            resume_ids = seed_resumes(conn, users)
            seed_logs(conn, users, job_ids, resume_ids)
            verify(conn)
        report_counts(conn, "after")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
