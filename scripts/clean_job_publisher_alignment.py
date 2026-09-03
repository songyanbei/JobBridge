"""将拟真数据库中的岗位地点与发布方经营主体统一到同一主地点。

该脚本只用于本地/演示数据清洗：
* 每个发布账号选择一个主城市（演示岗位优先保留其城市，否则按岗位数量最多的城市）；
* 发布方公司名、经营地址、岗位 city/district/address 统一到该主地点；
* 岗位级 hiring_company 与发布方公司保持一致；
* 同步修正岗位原始描述中的旧城市/旧地址，避免页面展示自相矛盾。

生产数据不应直接执行；生产场景应由业务审核后逐条修正。
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pymysql
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV = dotenv_values(ROOT / ".env")

COMPANY_CITY_SHORT = {
    "苏州市": "苏州", "无锡市": "无锡", "常州市": "常州", "南京市": "南京", "南通市": "南通",
    "上海市": "上海", "杭州市": "杭州", "宁波市": "宁波", "嘉兴市": "嘉兴",
    "深圳市": "深圳", "东莞市": "东莞", "广州市": "广州", "佛山市": "佛山", "中山市": "中山",
    "厦门市": "厦门", "泉州市": "泉州", "合肥市": "合肥", "徐州市": "徐州", "盐城市": "盐城",
    "金华市": "金华", "惠州市": "惠州", "汕头市": "汕头", "福州市": "福州",
}

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
    "徐州市": ["铜山区", "云龙区", "鼓楼区"],
    "盐城市": ["亭湖区", "盐都区", "大丰区"],
    "金华市": ["金东区", "义乌市", "永康市", "兰溪市"],
    "惠州市": ["惠城区", "惠阳区", "博罗县"],
    "汕头市": ["金平区", "龙湖区", "潮南区"],
    "福州市": ["仓山区", "闽侯县", "长乐区", "马尾区"],
}


def _connect():
    return pymysql.connect(
        host=os.getenv("DB_HOST", ENV.get("DB_HOST", "127.0.0.1")),
        port=int(os.getenv("DB_PORT", ENV.get("DB_PORT", 3306))),
        user=os.getenv("DB_USER", ENV.get("DB_USER", "jobbridge")),
        password=os.getenv("DB_PASSWORD", ENV.get("DB_PASSWORD", "jobbridge")),
        database=os.getenv("DB_NAME", ENV.get("DB_NAME", "jobbridge")),
        charset="utf8mb4",
        autocommit=False,
    )


def _short(city: str) -> str:
    return COMPANY_CITY_SHORT.get(city, city.replace("市", ""))


def _normalize_company(company: str | None, city: str) -> str:
    short = _short(city)
    company = (company or "").strip()
    if not company:
        return f"{short}众成人力资源服务有限公司"
    for source in sorted(set(COMPANY_CITY_SHORT.values()), key=len, reverse=True):
        if company.startswith(source):
            return short + company[len(source):]
    return short + company


def _district_from_address(address: str | None, city: str) -> str:
    address = address or ""
    for district in DISTRICTS_BY_CITY.get(city, []):
        if district in address:
            return district
    return DISTRICTS_BY_CITY.get(city, ["工业园区"])[0]


def _publisher_address(city: str, district: str, seed: int) -> str:
    street_names = ["工业大道", "科技路", "建设路", "兴业街", "现代大道", "创业路"]
    street = street_names[seed % len(street_names)]
    door = (seed * 37) % 900 + 50
    return f"{city}{district}{street} {door} 号"


def main() -> None:
    db = _connect()
    try:
        with db.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT external_userid, role, company, address FROM user")
            users = {row["external_userid"]: row for row in cur.fetchall()}
            cur.execute(
                "SELECT id, owner_userid, city, district, address, hiring_company, raw_text, description, extra "
                "FROM job ORDER BY id"
            )
            jobs = cur.fetchall()

        jobs_by_owner: dict[str, list[dict]] = defaultdict(list)
        for job in jobs:
            jobs_by_owner[job["owner_userid"]].append(job)

        owner_plan: dict[str, tuple[str, str, str, str]] = {}
        for owner, owner_jobs in jobs_by_owner.items():
            user = users.get(owner, {})
            for job in owner_jobs:
                if isinstance(job.get("extra"), str):
                    try:
                        job["extra"] = json.loads(job["extra"])
                    except (TypeError, ValueError):
                        job["extra"] = {}
            demo_cities = [
                j["city"] for j in owner_jobs
                if isinstance(j.get("extra"), dict) and j["extra"].get("demo_tag") == "demo_supp_v1"
            ]
            if demo_cities:
                city = Counter(demo_cities).most_common(1)[0][0]
            else:
                city = Counter(j["city"] for j in owner_jobs).most_common(1)[0][0]
            district = _district_from_address(user.get("address"), city)
            company = _normalize_company(user.get("company"), city)
            address = user.get("address") or ""
            if not address.startswith(city) or district not in address:
                address = _publisher_address(city, district, sum(ord(ch) for ch in owner))
            owner_plan[owner] = (city, district, company, address)

        with db.cursor() as cur:
            for owner, (city, district, company, address) in owner_plan.items():
                cur.execute(
                    "UPDATE user SET company=%s, address=%s WHERE external_userid=%s",
                    (company, address, owner),
                )
                for job in jobs_by_owner[owner]:
                    old_city = job["city"] or ""
                    old_address = job["address"] or ""
                    raw_text = job["raw_text"] or ""
                    description = job["description"] or ""
                    if old_address:
                        raw_text = raw_text.replace(old_address, address)
                        description = description.replace(old_address, address)
                    if old_city != city:
                        raw_text = raw_text.replace(old_city, city)
                        description = description.replace(old_city, city)
                    if city not in raw_text:
                        raw_text = f"{raw_text}，工作地点：{city}"
                    if city not in description:
                        description = f"{description}，工作地点：{city}"
                    if address not in raw_text:
                        raw_text = f"{raw_text}，地址：{address}"
                    if address not in description:
                        description = f"{description}，地址：{address}"
                    cur.execute(
                        "UPDATE job SET city=%s, district=%s, address=%s, hiring_company=%s, "
                        "raw_text=%s, description=%s WHERE id=%s",
                        (city, district, address, company, raw_text, description, job["id"]),
                    )
        db.commit()
        print(json.dumps({"owners": len(owner_plan), "jobs": len(jobs), "status": "ok"}, ensure_ascii=False))
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
