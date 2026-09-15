"""群接龙业务逻辑核心"""

import base64
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://apipro.qunjielong.com"

CATEGORY_MAP = {
    368: "食品饮料", 369: "生鲜果蔬", 370: "家居百货", 371: "厨房日用",
    374: "服饰鞋包", 377: "珠宝饰品", 380: "保健健康", 379: "鲜花绿植",
    376: "文创礼品", 375: "收纳家居", 372: "虚拟服务", 378: "母婴亲子",
    381: "美妆护肤",
}
ALL_CATEGORY_IDS = list(CATEGORY_MAP.keys())


def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def make_headers(config):
    return {
        "Host": "apipro.qunjielong.com",
        "Authorization": config["auth_token"],
        "appid": "wx059cd327295ab444",
        "client-version": "6.2.77",
        "companyId": "190",
        "content-type": "application/json",
        "device-type": "10",
        "feature-tag": "f0000",
        "mini-route": "pro/pages/data-analyse/data-analyse-v2-overview-page/data-analyse-v2-overview-page",
        "sceneCode": "1008",
        "uid": config["uid"],
        "version": "3.0.0",
        "charset": "utf-8",
        "Referer": "https://servicewechat.com/wx059cd327295ab444/2320/page-frame.html",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 12; SM-F926U Build/V417IR; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/150.0.7871.189 "
            "Safari/537.36 XWEB/1500117 MMWEBSDK/20260604 MMWEBID/1213 "
            "MicroMessenger/8.0.77.3160(0x28004D30) WeChat/arm64 Weixin NetType/WIFI "
            "Language/zh_CN ABI/arm64 MiniProgramEnv/android"
        ),
    }


def token_info(config):
    try:
        token = config.get("auth_token", "")
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp_ts = decoded.get("exp", 0)
        exp_dt = datetime.fromtimestamp(exp_ts)
        remaining = (exp_dt - datetime.now()).total_seconds()
        return {
            "uid": decoded.get("uid"),
            "exp_str": exp_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "remaining_seconds": int(remaining),
            "valid": remaining > 0,
        }
    except Exception as e:
        return {"valid": False, "error": str(e), "remaining_seconds": 0}


def dedup_by_goods(items):
    groups = defaultdict(list)
    for item in items:
        goods_ids = frozenset(
            g["goodsId"] for g in item.get("basicFeedGoodDTOList", []) if g.get("goodsId")
        )
        if not goods_ids:
            goods_ids = frozenset([item["actId"]])
        total_earn = sum(
            g.get("followEarning", {}).get("maxEarning", 0)
            for g in item.get("basicFeedGoodDTOList", [])
        )
        groups[goods_ids].append((total_earn, item))
    result = []
    for act_list in groups.values():
        act_list.sort(key=lambda x: x[0], reverse=True)
        result.append(act_list[0][1])
    return result


def parse_item(item):
    goods_list = item.get("basicFeedGoodDTOList", [])
    first_goods = goods_list[0] if goods_list else {}
    try:
        earning_val = float(first_goods.get("earning", 0) or 0)
    except (ValueError, TypeError):
        earning_val = 0
    follow_earning = first_goods.get("followEarning", {})
    max_follow = follow_earning.get("maxEarning", 0) / 100 if follow_earning else 0
    try:
        price = float(first_goods.get("groupBuyPrice", 0) or 0)
    except (ValueError, TypeError):
        price = 0
    stats = item.get("actStat", {})
    start_ts = item.get("effectTime", 0) or item.get("startTime", 0) or 0
    return {
        "actId": item.get("actId"),
        "actName": (item.get("actName", "") or "").replace("​", "").replace("‌", "").strip()[:80],
        "ghName": item.get("ghName", ""),
        "starGh": item.get("starGh", False),
        "price": price,
        "earning": earning_val,
        "followEarning": max_follow,
        "commissionPct": first_goods.get("commissionPercent", ""),
        "totalOrders": stats.get("totalOrderNum", 0),
        "helpSaleCount": item.get("helpSaleCount", 0),
        "goodsName": (first_goods.get("goodsName", "") or "")[:50],
        "hadHelpSale": item.get("hadHelpSale", False),
        "isFollow": item.get("isFollow", False),
        "actPic": item.get("actPic", ""),
        "startTime": start_ts,
        "startTimeStr": datetime.fromtimestamp(start_ts / 1000).strftime("%m-%d") if start_ts else "",
        "upStreamType": item.get("upStreamType", 0),
    }


def _fetch_one_page(session, headers, gh_id, page, category_ids, upstream_types, start_ts, end_ts):
    """拉单页 feed list，返回 (page, items, has_next) 或抛异常"""
    for attempt in range(3):
        try:
            resp = session.post(
                f"{BASE_URL}/help-sale-square/v6/relation_help_sale_feed/list",
                json={
                    "page": page, "sortType": 3, "pageSize": 10,
                    "ghId": gh_id, "foldSameAct": True,
                    "firstCategoryIdList": category_ids,
                    "pageParam": "",
                    "filterFeedDTO": {
                        "upStreamTypeList": upstream_types,
                        "starTime": start_ts,
                        "endTime": end_ts,
                    },
                },
                headers=headers, timeout=15,
            )
            data = resp.json()
            if data.get("code") != 200:
                raise ValueError(f"API错误: {data.get('msg', data)}")
            feed = data["data"]
            return page, feed.get("groupHelpSaleDTOs", []), feed.get("hasNextPage", False)
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                raise


def _fetch_details_batch(session, headers, gh_id, act_ids):
    """批量拉详情，返回 detail list"""
    for attempt in range(3):
        try:
            resp = session.post(
                f"{BASE_URL}/help-sale-square/feed_base/feed_base_search/query_list_by_act_ids",
                json={
                    "actIds": act_ids, "ghId": gh_id,
                    "returnDataList": [10, 20, 40, 50, 80],
                    "filterParam": json.dumps({
                        "filterSensitive": False, "filterFollowSupplyAct": False,
                        "filterSamePact": False, "filterBlackGh": False,
                        "filterOffShelvesAct": False, "filterStartGroupAct": True,
                    }),
                    "extraParam": json.dumps({"from": 6}),
                },
                headers=headers, timeout=15,
            )
            data = resp.json()
            return data.get("data", []) if data.get("code") == 200 else []
        except Exception:
            if attempt < 2:
                time.sleep(1.5 ** attempt)
            else:
                return []


def fetch_activities(config, q, category_ids=None, upstream_types=None, days_range=7, log_entries=None):
    """并发拉取帮卖活动，通过队列 q 推送进度事件"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    headers = make_headers(config)
    gh_id = config["gh_id"]
    category_ids = category_ids or ALL_CATEGORY_IDS
    upstream_types = upstream_types or [2, 3]

    now = datetime.now()
    start_ts = int((now - timedelta(days=days_range)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ts = int((now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    # Step 1: 先拉第1页，确认总页数范围
    session = make_session()
    q.put({"type": "page", "page": 1, "total": 0})
    try:
        _, first_items, has_next = _fetch_one_page(
            session, headers, gh_id, 1, category_ids, upstream_types, start_ts, end_ts)
    except Exception as e:
        q.put({"type": "error", "msg": f"首页请求失败: {e}"})
        _finish_fetch(q, [])
        return

    all_page_items = {1: first_items}
    if not first_items:
        _finish_fetch(q, [])
        return

    q.put({"type": "progress", "page": 1, "added": len(first_items), "total": len(first_items)})

    # Step 2: 并发拉剩余页（每批5页）
    if has_next:
        BATCH = 5
        page_num = 2
        while True:
            batch_pages = list(range(page_num, page_num + BATCH))
            q.put({"type": "page", "page": f"{batch_pages[0]}-{batch_pages[-1]}",
                   "total": sum(len(v) for v in all_page_items.values())})

            futures = {}
            with ThreadPoolExecutor(max_workers=BATCH) as ex:
                for p in batch_pages:
                    s = make_session()
                    futures[ex.submit(_fetch_one_page, s, headers, gh_id, p,
                                      category_ids, upstream_types, start_ts, end_ts)] = p

                batch_has_next = False
                for fut in as_completed(futures):
                    try:
                        pg, items, hn = fut.result()
                        all_page_items[pg] = items
                        if hn:
                            batch_has_next = True
                    except Exception as e:
                        q.put({"type": "error", "msg": f"Page 请求失败: {e}"})

            total_so_far = sum(len(v) for v in all_page_items.values())
            q.put({"type": "progress", "page": f"~{page_num+BATCH-1}",
                   "added": total_so_far - len(first_items), "total": total_so_far})

            if not batch_has_next:
                break
            page_num += BATCH

    # 去重 actIds，按页序收集
    seen_ids = set()
    ordered_act_ids = []
    for pg in sorted(all_page_items.keys()):
        for item in all_page_items[pg]:
            aid = item["actId"]
            if aid not in seen_ids:
                seen_ids.add(aid)
                ordered_act_ids.append(aid)

    total_acts = len(ordered_act_ids)
    q.put({"type": "page", "page": "详情", "total": total_acts})

    # Step 3: 并发拉详情（每批20个 actId，5并发）
    DETAIL_BATCH = 20
    DETAIL_WORKERS = 5
    all_items = []
    batches = [ordered_act_ids[i:i+DETAIL_BATCH]
               for i in range(0, len(ordered_act_ids), DETAIL_BATCH)]

    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
        futs = {ex.submit(_fetch_details_batch, make_session(), headers, gh_id, b): b
                for b in batches}
        done_count = 0
        for fut in as_completed(futs):
            details = fut.result()
            all_items.extend(details)
            done_count += len(futs[fut])
            q.put({"type": "progress", "page": "详情",
                   "added": len(details), "total": done_count,
                   "grand_total": total_acts})

    _finish_fetch(q, all_items, log_entries)


def _finish_fetch(q, all_items, log_entries=None):
    now = datetime.now()

    # last successful open per actId from log
    last_opened = {}
    for entry in (log_entries or []):
        aid = entry.get("actId")
        if not aid or entry.get("status") != "ok":
            continue
        try:
            t = datetime.strptime(entry["time"], "%Y-%m-%d %H:%M:%S")
            if aid not in last_opened or t > last_opened[aid]:
                last_opened[aid] = t
        except Exception:
            pass

    # split: new vs re-openable
    new_raw = [p for p in all_items if not p.get("hadHelpSale") and not p.get("isFollow")]
    reopen_raw = []
    for p in all_items:
        if not p.get("hadHelpSale"):
            continue
        if p.get("actStat", {}).get("totalOrderNum", 0) < 1:
            continue
        lo = last_opened.get(p.get("actId"))
        if lo and (now - lo).days < 5:
            continue
        reopen_raw.append(p)

    new_deduped = dedup_by_goods(new_raw)
    reopen_deduped = dedup_by_goods(reopen_raw)

    # best totalOrders per goodsName across ALL marketplace items
    goods_best = {}  # goodsName -> (totalOrders, actId)
    for item in all_items:
        gn = (item.get("basicFeedGoodDTOList") or [{}])[0].get("goodsName", "") or ""
        gn = gn[:50]
        orders = item.get("actStat", {}).get("totalOrderNum", 0)
        if gn and (gn not in goods_best or orders > goods_best[gn][0]):
            goods_best[gn] = (orders, item.get("actId"))

    def annotate_and_sort(items, category):
        result = []
        for item in items:
            p = parse_item(item)
            p["category"] = category
            gn = p.get("goodsName", "")
            best_orders, best_id = goods_best.get(gn, (0, None))
            if best_id and best_id != p["actId"] and best_orders > p["totalOrders"]:
                p["bestActId"] = best_id
                p["bestOrders"] = best_orders
            else:
                p["bestActId"] = None
                p["bestOrders"] = None
            lo = last_opened.get(p["actId"])
            p["daysSinceOpen"] = (now - lo).days if lo else None
            result.append(p)
        return sorted(result, key=lambda x: x.get("startTime", 0), reverse=True)

    new_items = annotate_and_sort(new_deduped, "new")
    reopen_items = annotate_and_sort(reopen_deduped, "reopen")

    q.put({
        "type": "done",
        "total_raw": len(all_items),
        "removed_helped": len(all_items) - len(new_raw),
        "removed_dup": len(new_raw) - len(new_deduped),
        "total_available": len(new_items) + len(reopen_items),
        "reopen_count": len(reopen_items),
        "items": new_items + reopen_items,
    })


def build_release_payload(template, parent_act_id, gh_id):
    act_dto = template["activityDTO"]
    act_info = template["activityInfoDTO"]
    act_settings = template["activitySettingsDTO"]
    act_goods = template["activityGoodsList"]
    act_logistics = template["actLogisticsInfos"]
    svc_guarantee = template["servicesGuarantee"]

    act_goods_params = []
    for goods in act_goods:
        sku_list = goods.get("mutiActGoodsInfoDTO", {})
        sku_params = [
            {
                "goodsId": sku["goodsId"],
                "startActGoodsId": sku["startActGoodsId"],
                "goodsPicList": sku.get("goodsPicList", []),
                "groupBuyPrice": sku["groupBuyPrice"],
                "originalPrice": sku.get("originalPrice", 0),
                "costPrice": sku["costPrice"],
                "commissionPercent": None,
                "totalStock": sku["totalStock"],
                "useStock": sku["useStock"],
                "goodsTotalStock": sku["goodsTotalStock"],
                "goodsUseStock": sku["goodsUseStock"],
                "goodsDimensionInfoList": [
                    {
                        "goodsDimensionId": d["goodsDimensionId"],
                        "goodsDimensionValueId": d["goodsDimensionValueId"],
                        "goodsDimensionFrontId": d.get("actGoodsDimensionId", 0),
                        "goodsDimensionValueFrontId": d.get("actGoodsDimensionValueId", 0),
                    }
                    for d in sku.get("goodsDimensionInfoList", [])
                ],
                "goodsWeight": sku.get("goodsWeight", 0),
                "supplyPrice": sku.get("supplyPrice", 0),
                "actGoodsStatus": sku.get("actGoodsStatus", 10),
                "goodsCost": sku.get("goodsCost", 0),
                "leftStock": sku.get("leftStock", 0),
                "followCommissionPercent": sku.get("followCommissionPercent", 0),
                "followHsCommission": sku.get("followHsCommission", 0),
                "followActCostPrice": sku.get("followActCostPrice", 0),
                "followProfit": sku.get("followProfit", 0),
                "useStockCount": sku.get("useStockCount", 0),
                "minLeftStock": sku.get("minLeftStock", 0),
                "goodsSupplyPrice": sku.get("goodsSupplyPrice", 0),
                "evaluationPrice": 0,
            }
            for sku in sku_list.get("actGoodsSkuPropertyDTOS", [])
        ]
        dim_list = [
            {
                "goodsDimensionId": d["goodsDimensionId"],
                "dimensionName": d["dimensionName"],
                "dimensionValueList": [
                    {
                        "goodsDimensionValueId": dv["goodsDimensionValueId"],
                        "valueName": dv["valueName"],
                        "dimensionValueDeleteFlag": dv.get("dimensionValueDeleteFlag", 0),
                        "goodsDimensionValueFrontId": dv.get("actGoodsDimensionValueId", 0),
                    }
                    for dv in d.get("dimensionValueList", [])
                ],
                "dimensionDeleteFlag": d.get("dimensionDeleteFlag", 0),
                "goodsDimensionFrontId": d.get("actGoodsDimensionId", 0),
            }
            for d in sku_list.get("dimensionList", [])
        ]
        act_goods_params.append({
            "actGoodsIntro": goods.get("actGoodsIntro", ""),
            "actGoodsName": goods.get("actGoodsName", ""),
            "actGoodsPicList": goods.get("actGoodsPicList", []),
            "actGoodsUnit": goods.get("actGoodsUnit", ""),
            "payLimitCount": goods.get("payLimitCount", 0),
            "payLimitModel": goods.get("payLimitModel", 10),
            "goodsCategoryId": goods.get("goodsCategoryId", 0),
            "goodsId": goods["goodsId"],
            "actGoodsStatus": goods.get("actGoodsStatus", 10),
            "startActGoodsId": goods.get("startActGoodsId", 0),
            "showGoodsAllOrder": goods.get("showGoodsAllOrder", 5),
            "showGoodsGroupNum": goods.get("showGoodsGroupNum", 5),
            "actGroupBuyPrice": goods.get("actGroupBuyPrice", 0),
            "totalStock": goods.get("totalStock", 0),
            "actGoodsExtStatus": goods.get("actGoodsExtStatus", 10),
            "priceLimitStart": goods.get("priceLimitStart", 10),
            "priceLimitEnd": goods.get("priceLimitEnd", 0),
            "dimensionModel": goods.get("dimensionModel", 10),
            "mutiActGoodsInfoParam": {"actGoodsSkuParam": sku_params, "dimensionList": dim_list},
            "evaluationQuota": None,
            "notDiscount": goods.get("notDiscount", 10),
        })

    return {
        "actDiscountParam": {"discountItemParams": []},
        "actGhGroupIdList": [],
        "actSettingsParam": {
            "richTextShowMode": act_settings.get("richTextShowMode", 20),
            "goodsSelectLimitCount": act_settings.get("goodsSelectLimitCount", 0),
            "goodsSelectLimitModel": act_settings.get("goodsSelectLimitModel", 10),
            "activityTpiLimitCount": act_settings.get("activityTpiLimitCount", 0),
            "activityTpiLimitModel": act_settings.get("activityTpiLimitModel", 10),
            "tpiLeastAmount": act_settings.get("tpiLeastAmount", 0),
            "tpiLeastAmountModel": act_settings.get("tpiLeastAmountModel", 10),
            "tpiUserPrivacyType": act_settings.get("tpiUserPrivacyType", 60),
            "tpiImportantTips": act_settings.get("tpiImportantTips", ""),
            "contractSelect": act_settings.get("contractSelect", 10),
            "phoneSelect": act_settings.get("phoneSelect", 10),
            "addressSelect": act_settings.get("addressSelect", 10),
            "redPacketRecord": act_settings.get("redPacketRecord", 5),
            "shardRewardRecord": act_settings.get("shardRewardRecord", 5),
            "allowSignIn": act_settings.get("allowSignIn", 5),
            "actRedemptionSettingId": act_settings.get("actRedemptionSettingId", 0),
            "titleEditModel": act_settings.get("titleEditModel", 20),
            "synchronizedModel": act_settings.get("synchronizedModel", 10),
            "allowGroupSelectGoods": act_settings.get("allowGroupSelectGoods", 5),
            "allowCancelOrder": act_settings.get("allowCancelOrder", 5),
            "parentSettlingModel": act_settings.get("parentSettlingModel", 40),
            "groupImportantTips": act_settings.get("groupImportantTips", ""),
            "transmitModel": act_settings.get("transmitModel", 10),
            "payJoinModel": act_settings.get("payJoinModel", 10),
            "carriageTemplateId": act_settings.get("carriageTemplateId", 0),
            "lowerDeliverySw": act_settings.get("lowerDeliverySw", 5),
            "showGoodsWeightToCustomer": act_settings.get("showGoodsWeightToCustomer", 5),
            "carriageCollectGhId": act_settings.get("carriageCollectGhId", ""),
            "materialIds": act_settings.get("materialIds", []),
            "allowFollow": act_settings.get("allowFollow", 5),
            "featureVersion": act_settings.get("featureVersion", 5),
            "syncRootTypes": act_settings.get("syncChildTypes", act_settings.get("syncRootTypes", {})),
            "actRecommendTypes": act_settings.get("actRecommendTypes", []),
            "showGoodsStockToCustomer": act_settings.get("showGoodsStockToCustomer", 5),
            "onlyFirstOrderRequireFeeSwitch": act_settings.get("onlyFirstOrderRequireFeeSwitch", False),
            "exchangeSwitch": None,
            "syncRootContextTypes": [10, 20, 30],
            "promoterSw": 5,
            "gactSw": 5,
            "groupSalerSelectModel": 10,
            "syncRestartSw": 10,
            "returnAddrType": 0,
            "recommendActIds": [],
            "childExchangeSwitch": 5,
        },
        "activityAttrList": [],
        "activityInfoParam": {
            "activityDetail": act_info.get("activityDetail", ""),
            "bgImgId": act_info.get("bgImgId", 0),
        },
        "addActParam": {
            "ghViewType": act_dto.get("ghViewType", 10),
            "activityName": act_dto["activityName"],
            "activityStatus": 10,
            "startTime": act_dto["startTime"],
            "endTime": act_dto["endTime"],
            "ghId": gh_id,
            "activityModel": act_dto.get("activityModel", 20),
            "isDoubtSubmit": False,
            "isCertificationSubmit": False,
            "logisticsModel": act_dto.get("logisticsModel", 10),
            "displayType": act_dto.get("displayType", 10),
            "copyScene": 10,
            "startActId": parent_act_id,
        },
        "actGoodsParams": act_goods_params,
        "isSelectAllGroup": 1,
        "sourceInfo": {"tag": "", "data": "", "infos": ""},
        "isDoubtSubmit": False,
        "isCertificationSubmit": False,
        "actLogisticsInfos": [
            {
                "logisticsModel": li.get("logisticsModel", 10),
                "actLogisticsAttrs": [
                    {"attrType": a["attrType"], "attrName": a["attrName"]}
                    for a in li.get("actLogisticsAttrs", [])
                ],
            }
            for li in act_logistics
        ],
        "servicesGuarantee": {
            "guaranteeStatus": svc_guarantee.get("guaranteeStatus", 10),
            "id": svc_guarantee.get("id", 0),
            "deliveryModel": svc_guarantee.get("deliveryModel", 10),
            "deliveryLogistics": svc_guarantee.get("deliveryLogistics", ""),
            "deliveryPlace": svc_guarantee.get("deliveryPlace", ""),
            "customerServiceRemark": svc_guarantee.get("customerServiceRemark", ""),
            "timelinessModel": svc_guarantee.get("timelinessModel", 10),
            "deliveryDeadline": svc_guarantee.get("deliveryDeadline", 3),
            "allowEditCsRemarkSw": svc_guarantee.get("allowEditCsRemarkSw", 10),
        },
        "startSelectFollowSettings": {
            "followSelectMode": 0, "backwardOption": True, "followGhIdList": [],
        },
        "labelIdList": [],
    }


def open_one_group(config, parent_act_id, dry_run=False):
    """开单个团。dry_run=True 只拉模板不提交。返回 (ok, new_act_id, act_name, error)"""
    session = make_session()
    headers = make_headers(config)
    gh_id = config["gh_id"]

    url = f"{BASE_URL}/activity-biz/help_sale_root/act/for_follow/{parent_act_id}/{gh_id}"
    resp = session.get(url, headers=headers, timeout=15)
    data = resp.json()
    if data.get("code") != 200:
        return False, None, "", f"获取模板失败: {data.get('msg', data)}"

    template = data["data"]
    act_name = template["activityDTO"]["activityName"]

    if dry_run:
        return True, None, act_name, ""

    payload = build_release_payload(template, parent_act_id, gh_id)
    result = session.post(
        f"{BASE_URL}/activity-biz/help_sale_root/act/release",
        json=payload, headers=headers, timeout=30,
    ).json()

    if result.get("code") != 200:
        return False, None, act_name, f"开团失败: {result.get('msg', result)}"

    new_act_id = result["data"]["actId"]

    try:
        session.post(
            f"{BASE_URL}/activity-biz/try_order/act/release",
            json={"ghGroupId": gh_id, "pactId": new_act_id, "sysGeneratedType": 90},
            headers=headers, timeout=15,
        )
    except Exception:
        pass

    return True, new_act_id, act_name, ""


def batch_open_groups(config, act_ids, q, dry_run=False, delay=1.0):
    """批量开团，通过队列 q 推送进度"""
    total = len(act_ids)
    success = 0
    fail = 0

    for i, act_id in enumerate(act_ids):
        try:
            ok, new_id, act_name, err = open_one_group(config, act_id, dry_run)
            entry = {
                "type": "item",
                "index": i + 1,
                "total": total,
                "actId": act_id,
                "actName": act_name[:50],
                "ok": ok,
                "newActId": new_id,
                "error": err,
                "dryRun": dry_run,
            }
            if ok:
                success += 1
            else:
                fail += 1
            q.put(entry)
        except Exception as e:
            fail += 1
            q.put({
                "type": "item", "index": i + 1, "total": total,
                "actId": act_id, "actName": "", "ok": False,
                "newActId": None, "error": str(e), "dryRun": dry_run,
            })

        if i < total - 1:
            time.sleep(delay)

    q.put({"type": "done", "success": success, "fail": fail, "total": total})
