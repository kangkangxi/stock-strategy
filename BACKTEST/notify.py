from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request


def send_report(report_path, config_path, state_dir):
    raw = Path(report_path).read_bytes()
    content = raw.decode("utf-8").strip()
    if not content:
        raise ValueError("报告为空，未发送")
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("飞书配置必须为JSON对象，未发送")
    if config.get("enabled") is not True:
        return {"status": "disabled"}
    webhook = config.get("webhook", "")
    if not isinstance(webhook, str) or not re.fullmatch(
            r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", webhook):
        raise ValueError("飞书配置无效，未发送")
    head = content.split("三、完整平仓明细", 1)[0]
    boundary = content.split("六、解释边界（必须与收益一同阅读）", 1)
    if len(boundary) != 2:
        raise ValueError("报告缺少解释边界，未发送")
    text = content
    content_mode = "full"
    body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    if len(body) > 20000:
        text = head + "详细成交与资金曲线保存在本机配套TXT/JSON。\n\n六、解释边界（必须与收益一同阅读）" + boundary[1]
        content_mode = "summary_with_limitations"
        body = json.dumps({"msg_type": "text", "content": {"text": text}}, ensure_ascii=False).encode("utf-8")
    if len(body) > 20000:
        raise ValueError("飞书正文超过20KB，未截去风险说明，未发送")
    key = hashlib.sha256(raw).hexdigest()
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / (key + ".json")
    started = {"status": "started", "report_sha256": key, "content_mode": content_mode, "at": datetime.now(timezone.utc).isoformat()}
    try:
        with receipt.open("x", encoding="utf-8") as handle:
            json.dump(started, handle, ensure_ascii=False)
    except FileExistsError:
        return {"status": "not_resent", "previous": json.loads(receipt.read_text(encoding="utf-8"))["status"], "report_sha256": key}
    request = urllib.request.Request(webhook, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read(65536).decode("utf-8"))
        code = result.get("code", result.get("StatusCode")) if isinstance(result, dict) else None
        outcome = {"status": "sent"} if type(code) is int and code == 0 else {
            "status": "failed", "error": "飞书返回非成功业务码；不自动重发"}
    except urllib.error.HTTPError as error:
        outcome = {"status": "failed", "error": f"飞书HTTP {error.code}；不自动重发"}
    except Exception:
        outcome = {"status": "unknown", "error": "发送或响应失败，是否送达未知；不自动重发"}
    outcome.update({"report_sha256": key, "content_mode": content_mode, "at": started["at"]})
    temporary = receipt.with_suffix(".tmp")
    temporary.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(receipt)
    return outcome
