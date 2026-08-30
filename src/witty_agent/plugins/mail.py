"""邮件业务工具：IMAP/SMTP 内网可配，不写死公网邮箱商。"""

from __future__ import annotations

import imaplib
import json
import mimetypes
import os
import re
import smtplib
import uuid
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Protocol

from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID, data_root
from witty_agent.logging import get_logger
from witty_agent.prompts import get_prompt
from witty_agent.email_config import public_email_fields, save_email_overlay, save_email_secrets
from witty_agent.layout import DEFAULT_AGENT_ID, DEFAULT_PROJECT_ID
from witty_agent.runtime import email_settings
from witty_agent.tools.registry import ToolSpec, register_tool

logger = get_logger("plugins.mail")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+")
_ACTION_RE = re.compile(
    r"(请(?:尽快|于|在|于本|于下)|烦请|务必|截止日期|截止时间|待办|"
    r"action required|please (?:review|confirm|reply|send)|deadline)",
    re.IGNORECASE,
)
_DRAFT_NAME = re.compile(r"^d-[0-9a-f]{8}$")


class MailBackend(Protocol):
    def list_messages(self, mailbox: str, limit: int, query: str = "") -> list[dict[str, Any]]: ...

    def fetch_message(self, mailbox: str, uid: str) -> dict[str, Any]: ...

    def send_message(self, payload: dict[str, Any]) -> str: ...


_OVERRIDE: MailBackend | None = None


def set_mail_backend(backend: MailBackend | None) -> None:
    global _OVERRIDE
    _OVERRIDE = backend


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drafts_dir() -> Path:
    configured = str(email_settings().get("drafts_dir") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return data_root() / DEFAULT_PROJECT_ID / "agents" / DEFAULT_AGENT_ID / "mail" / "drafts"


def _draft_path(draft_id: str) -> Path:
    if not _DRAFT_NAME.fullmatch(draft_id):
        raise ValueError(get_prompt("email_draft_bad_id", draft_id=draft_id))
    return drafts_dir() / f"{draft_id}.json"


def load_draft(draft_id: str) -> dict[str, Any]:
    path = _draft_path(draft_id)
    if not path.is_file():
        raise FileNotFoundError(get_prompt("email_draft_missing", draft_id=draft_id))
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(get_prompt("email_draft_missing", draft_id=draft_id))
    return data


def save_draft(draft: dict[str, Any]) -> dict[str, Any]:
    directory = drafts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    draft_id = str(draft.get("id") or "")
    if not _DRAFT_NAME.fullmatch(draft_id):
        draft_id = f"d-{uuid.uuid4().hex[:8]}"
        draft["id"] = draft_id
    draft["updated_at"] = _now()
    path = directory / f"{draft_id}.json"
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft


def list_drafts() -> list[dict[str, Any]]:
    directory = drafts_dir()
    if not directory.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("d-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("id"):
            rows.append(data)
    return rows


def _split_addrs(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        text = ",".join(str(item) for item in raw)
    else:
        text = str(raw)
    found = _EMAIL_RE.findall(text)
    return [item for item in found if item]


def analyze_message(payload: dict[str, Any]) -> dict[str, Any]:
    body = str(payload.get("body") or "")
    subject = str(payload.get("subject") or "")
    haystack = f"{subject}\n{body}"
    actions = []
    for line in haystack.splitlines():
        stripped = line.strip()
        if stripped and _ACTION_RE.search(stripped):
            actions.append(stripped[:200])
    attachments = payload.get("attachments") or []
    names = []
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str):
                names.append(item)
    kind = "notify"
    if actions:
        kind = "request"
    elif re.search(r"会议|纪要|agenda|meeting", haystack, re.IGNORECASE):
        kind = "meeting"
    return {
        "uid": payload.get("uid") or payload.get("id") or "",
        "from": payload.get("from") or "",
        "to": payload.get("to") or [],
        "cc": payload.get("cc") or [],
        "subject": subject,
        "kind": kind,
        "actions": actions[:12],
        "attachments": names,
        "chars": len(body),
        "preview": " ".join(body.split())[:240],
    }


def _format_row(row: dict[str, Any]) -> str:
    to_list = row.get("to") or []
    if isinstance(to_list, list):
        to_text = ", ".join(str(item) for item in to_list[:4])
    else:
        to_text = str(to_list)
    return get_prompt(
        "email_list_row",
        uid=str(row.get("uid") or row.get("id") or ""),
        date=str(row.get("date") or row.get("updated_at") or ""),
        sender=str(row.get("from") or ""),
        to=to_text or "-",
        subject=str(row.get("subject") or "(无主题)"),
    )


def _format_message(row: dict[str, Any]) -> str:
    analysis = analyze_message(row)
    attach = ", ".join(analysis["attachments"]) or "（无）"
    actions = "\n".join(f"- {item}" for item in analysis["actions"]) or "（无）"
    body = str(row.get("body") or "")
    limit = int(email_settings().get("max_body_chars") or 8000)
    if len(body) > limit:
        body = body[:limit] + "\n" + get_prompt("email_body_truncated", limit=str(limit))
    return get_prompt(
        "email_message_view",
        uid=str(analysis["uid"]),
        date=str(row.get("date") or row.get("updated_at") or ""),
        sender=str(row.get("from") or ""),
        to=", ".join(str(item) for item in (row.get("to") or [])) or "-",
        cc=", ".join(str(item) for item in (row.get("cc") or [])) or "-",
        subject=str(row.get("subject") or "(无主题)"),
        kind=str(analysis["kind"]),
        attachments=attach,
        actions=actions,
        body=body or "（空）",
    )


def _format_analysis(payload: dict[str, Any]) -> str:
    analysis = analyze_message(payload)
    actions = "\n".join(f"- {item}" for item in analysis["actions"]) or "（无）"
    return get_prompt(
        "email_analyze_view",
        uid=str(analysis["uid"]),
        kind=str(analysis["kind"]),
        sender=str(analysis["from"] or "-"),
        to=", ".join(str(item) for item in analysis["to"]) or "-",
        subject=str(analysis["subject"] or "(无主题)"),
        attachments=", ".join(analysis["attachments"]) or "（无）",
        actions=actions,
        preview=str(analysis["preview"] or "（空）"),
        chars=str(analysis["chars"]),
    )


def _header(msg: Any, name: str) -> str:
    value = msg.get(name)
    return str(value) if value else ""


def parse_raw_message(raw: bytes, *, uid: str, mailbox: str) -> dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = ""
    attachments: list[dict[str, Any]] = []
    if msg.is_multipart():
        for part in msg.walk():
            disposition = str(part.get_content_disposition() or "")
            filename = part.get_filename()
            if filename or disposition == "attachment":
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    {
                        "name": filename or "attachment",
                        "size": len(payload),
                        "bytes": payload,
                    }
                )
                continue
            if part.get_content_type() == "text/plain" and not body:
                text = part.get_content()
                body = text if isinstance(text, str) else str(text)
        if not body:
            body = msg.get_body(preferencelist=("plain", "html"))
            body = str(body.get_content()) if body is not None else ""
    else:
        content = msg.get_content()
        body = content if isinstance(content, str) else str(content)
    return {
        "uid": uid,
        "mailbox": mailbox,
        "from": _header(msg, "From"),
        "to": _split_addrs(_header(msg, "To")),
        "cc": _split_addrs(_header(msg, "Cc")),
        "subject": _header(msg, "Subject"),
        "date": _header(msg, "Date"),
        "body": body.strip(),
        "attachments": attachments,
    }


def _safe_query(query: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff @._+-]+", " ", query or "")
    return " ".join(cleaned.split())[:80]


class MemoryMailbox:
    """测试用内存信箱，不碰网络。"""

    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []

    def add(self, **fields: Any) -> dict[str, Any]:
        uid = str(fields.get("uid") or str(len(self.messages) + 1))
        row = {
            "uid": uid,
            "mailbox": str(fields.get("mailbox") or "INBOX"),
            "from": str(fields.get("from") or ""),
            "to": list(fields.get("to") or []),
            "cc": list(fields.get("cc") or []),
            "subject": str(fields.get("subject") or ""),
            "date": str(fields.get("date") or _now()),
            "body": str(fields.get("body") or ""),
            "attachments": _normalize_attachments(fields.get("attachments") or []),
        }
        self.messages[uid] = row
        return row

    def list_messages(self, mailbox: str, limit: int, query: str = "") -> list[dict[str, Any]]:
        needle = (query or "").casefold()
        rows = [
            dict(item)
            for item in self.messages.values()
            if not mailbox or item.get("mailbox") == mailbox
        ]
        if needle:
            rows = [
                item
                for item in rows
                if needle in str(item.get("subject") or "").casefold()
                or needle in str(item.get("body") or "").casefold()
                or needle in str(item.get("from") or "").casefold()
            ]
        rows.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        return rows[: max(1, limit)]

    def fetch_message(self, mailbox: str, uid: str) -> dict[str, Any]:
        row = self.messages.get(uid)
        if row is None or (mailbox and row.get("mailbox") != mailbox):
            raise FileNotFoundError(get_prompt("email_uid_missing", uid=uid))
        return dict(row)

    def send_message(self, payload: dict[str, Any]) -> str:
        self.sent.append(dict(payload))
        return get_prompt("email_send_ok", subject=str(payload.get("subject") or ""))


class StdlibMailbox:
    def list_messages(self, mailbox: str, limit: int, query: str = "") -> list[dict[str, Any]]:
        client = self._imap()
        try:
            status, _ = client.select(mailbox)
            if status != "OK":
                raise RuntimeError(get_prompt("email_failed", action="select", reason=mailbox))
            crit = "ALL"
            cleaned = _safe_query(query)
            if cleaned:
                crit = f'(OR SUBJECT "{cleaned}" TEXT "{cleaned}")'
            status, data = client.uid("SEARCH", None, crit)
            if status != "OK":
                raise RuntimeError(get_prompt("email_failed", action="search", reason=status))
            uids = (data[0] or b"").split()[-max(1, limit) :]
            rows: list[dict[str, Any]] = []
            for uid in reversed(uids):
                status, fetched = client.uid("FETCH", uid, "(RFC822.HEADER)")
                if status != "OK" or not fetched:
                    continue
                raw = _fetch_bytes(fetched)
                parsed = parse_raw_message(raw, uid=uid.decode("ascii", "replace"), mailbox=mailbox)
                parsed["body"] = ""
                rows.append(parsed)
            return rows
        finally:
            _imap_close(client)

    def fetch_message(self, mailbox: str, uid: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9]+", uid):
            raise ValueError(get_prompt("email_uid_missing", uid=uid))
        client = self._imap()
        try:
            status, _ = client.select(mailbox)
            if status != "OK":
                raise RuntimeError(get_prompt("email_failed", action="select", reason=mailbox))
            status, fetched = client.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not fetched:
                raise FileNotFoundError(get_prompt("email_uid_missing", uid=uid))
            raw = _fetch_bytes(fetched)
            if not raw:
                raise FileNotFoundError(get_prompt("email_uid_missing", uid=uid))
            return parse_raw_message(raw, uid=uid, mailbox=mailbox)
        finally:
            _imap_close(client)

    def send_message(self, payload: dict[str, Any]) -> str:
        cfg = email_settings()
        host = str(cfg.get("smtp_host") or "")
        if not host:
            raise RuntimeError(get_prompt("email_not_configured"))
        user = str(cfg.get("username") or "")
        password = str(cfg.get("smtp_password") or "")
        if not user or not password:
            raise RuntimeError(get_prompt("email_missing_credentials"))
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = ", ".join(payload.get("to") or [])
        cc = payload.get("cc") or []
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = str(payload.get("subject") or "")
        msg.set_content(str(payload.get("body") or ""))
        for item in payload.get("attachments") or []:
            if not isinstance(item, dict):
                continue
            path = Path(str(item.get("path") or ""))
            if not path.is_file():
                raise FileNotFoundError(get_prompt("email_attach_missing", path=str(path)))
            mime, _ = mimetypes.guess_type(path.name)
            main, sub = (mime or "application/octet-stream").split("/", 1)
            msg.add_attachment(
                path.read_bytes(),
                maintype=main,
                subtype=sub,
                filename=str(item.get("name") or path.name),
            )
        timeout = int(cfg.get("timeout_sec") or 20)
        port = int(cfg.get("smtp_port") or 465)
        if cfg.get("smtp_ssl"):
            client = smtplib.SMTP_SSL(host, port, timeout=timeout)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            if cfg.get("smtp_starttls"):
                client.starttls()
        try:
            client.login(user, password)
            recipients = list(payload.get("to") or []) + list(payload.get("cc") or []) + list(
                payload.get("bcc") or []
            )
            client.send_message(msg, from_addr=user, to_addrs=recipients)
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise RuntimeError(get_prompt("email_failed", action="smtp", reason=str(exc))) from exc
        finally:
            try:
                client.quit()
            except Exception:
                pass
        logger.info("邮件已发送 subject_len=%s to=%s", len(str(payload.get("subject") or "")), len(payload.get("to") or []))
        return get_prompt("email_send_ok", subject=str(payload.get("subject") or ""))

    def _imap(self) -> imaplib.IMAP4:
        cfg = email_settings()
        host = str(cfg.get("imap_host") or "")
        if not host:
            raise RuntimeError(get_prompt("email_not_configured"))
        user = str(cfg.get("username") or "")
        password = str(cfg.get("imap_password") or "")
        if not user or not password:
            raise RuntimeError(get_prompt("email_missing_credentials"))
        timeout = int(cfg.get("timeout_sec") or 20)
        port = int(cfg.get("imap_port") or 993)
        try:
            if cfg.get("imap_ssl"):
                client: imaplib.IMAP4 = imaplib.IMAP4_SSL(host, port, timeout=timeout)
            else:
                client = imaplib.IMAP4(host, port, timeout=timeout)
            client.login(user, password)
        except (imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            raise RuntimeError(get_prompt("email_failed", action="imap", reason=str(exc))) from exc
        return client


def _imap_close(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
    except Exception:
        pass


def _fetch_bytes(fetched: Any) -> bytes:
    chunks: list[bytes] = []
    for item in fetched:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            chunks.append(bytes(item[1]))
    return b"".join(chunks)


def mail_backend() -> MailBackend:
    if _OVERRIDE is not None:
        return _OVERRIDE
    return StdlibMailbox()


def _cfg_flag(value: object) -> str:
    return get_prompt("email_flag_yes") if value else get_prompt("email_flag_no")


def public_mail_snapshot(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """给 HTTP/桌面用的通道快照，不含密码正文。"""
    cfg = email_settings(project_id, agent_id, root=root)
    drafts = []
    for item in list_drafts():
        drafts.append(
            {
                "id": str(item.get("id") or ""),
                "to": list(item.get("to") or []),
                "subject": str(item.get("subject") or ""),
                "attachments": len(item.get("attachments") or []),
                "updated_at": str(item.get("updated_at") or ""),
            }
        )
    public = public_email_fields(cfg)
    public["drafts"] = drafts
    public["text"] = email_status(project_id, agent_id, root=root)
    return public


def save_mail_settings(
    payload: dict[str, Any],
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    """保存界面提交的通道。密码只进 vault。"""
    fields = {
        "imap_host": str(payload.get("imap_host") or "").strip(),
        "imap_port": int(payload.get("imap_port") or 993),
        "imap_ssl": bool(payload.get("imap_ssl", True)),
        "smtp_host": str(payload.get("smtp_host") or "").strip(),
        "smtp_port": int(payload.get("smtp_port") or 465),
        "smtp_ssl": bool(payload.get("smtp_ssl", True)),
        "smtp_starttls": bool(payload.get("smtp_starttls", False)),
        "username": str(payload.get("username") or "").strip(),
        "mailbox": str(payload.get("mailbox") or "INBOX").strip() or "INBOX",
    }
    if fields["imap_port"] <= 0 or fields["smtp_port"] <= 0:
        raise ValueError("imap_port/smtp_port must be positive")
    save_email_overlay(fields, project_id, agent_id, root=root)
    save_email_secrets(
        imap_password=str(payload.get("imap_password") or ""),
        smtp_password=str(payload.get("smtp_password") or ""),
        project_id=project_id,
        agent_id=agent_id,
        root=root,
    )
    return public_mail_snapshot(project_id, agent_id, root=root)


def probe_live() -> int:
    """检查内网邮箱是否已配置。缺主机或账号时返回 2，不连公网。"""
    cfg = email_settings()
    if not str(cfg.get("imap_host") or "").strip() or not str(cfg.get("smtp_host") or "").strip():
        print(get_prompt("email_not_configured"))
        return 2
    if not str(cfg.get("username") or "").strip() or not str(cfg.get("imap_password") or "").strip():
        print(get_prompt("email_missing_credentials"))
        return 2
    print(email_status())
    return 0


def email_status(
    project_id: str = DEFAULT_PROJECT_ID,
    agent_id: str = DEFAULT_AGENT_ID,
    *,
    root: Path | None = None,
) -> str:
    """查看当前 IMAP/SMTP 通道和本地草稿，不读密码。"""
    cfg = email_settings(project_id, agent_id, root=root)
    drafts = list_drafts()
    backend = "memory" if _OVERRIDE is not None else "stdlib"
    return get_prompt(
        "email_status_view",
        imap_host=str(cfg.get("imap_host") or get_prompt("email_unset")),
        imap_port=str(cfg.get("imap_port") or ""),
        imap_ssl=_cfg_flag(cfg.get("imap_ssl")),
        smtp_host=str(cfg.get("smtp_host") or get_prompt("email_unset")),
        smtp_port=str(cfg.get("smtp_port") or ""),
        smtp_ssl=_cfg_flag(cfg.get("smtp_ssl")),
        smtp_starttls=_cfg_flag(cfg.get("smtp_starttls")),
        username=str(cfg.get("username") or get_prompt("email_unset")),
        imap_password=get_prompt("email_password_set") if cfg.get("imap_password") else get_prompt("email_password_unset"),
        smtp_password=get_prompt("email_password_set") if cfg.get("smtp_password") else get_prompt("email_password_unset"),
        mailbox=str(cfg.get("mailbox") or "INBOX"),
        drafts_dir=str(drafts_dir()),
        draft_count=str(len(drafts)),
        backend=backend,
    )


def email_list(mailbox: str = "", limit: int = 0, query: str = "") -> str:
    """列出收件箱或指定邮箱的最近邮件。"""
    cfg = email_settings()
    box = mailbox.strip() or str(cfg.get("mailbox") or "INBOX")
    cap = int(cfg.get("max_list") or 20)
    take = int(limit) if int(limit) > 0 else cap
    take = min(max(take, 1), cap)
    try:
        rows = mail_backend().list_messages(box, take, query=query.strip())
    except (RuntimeError, OSError, TimeoutError) as exc:
        return str(exc)
    if not rows:
        return get_prompt("email_list_empty", mailbox=box)
    body = "\n".join(_format_row(item) for item in rows)
    return get_prompt("email_list_view", mailbox=box, count=str(len(rows)), rows=body)


def email_read(uid: str, mailbox: str = "") -> str:
    """按 UID 读取一封邮件全文并附带结构摘要。"""
    cfg = email_settings()
    box = mailbox.strip() or str(cfg.get("mailbox") or "INBOX")
    try:
        row = mail_backend().fetch_message(box, uid.strip())
    except (RuntimeError, FileNotFoundError, ValueError, OSError, TimeoutError) as exc:
        return str(exc)
    return _format_message(row)


def email_analyze(uid: str = "", draft_id: str = "", text: str = "", mailbox: str = "") -> str:
    """分析一封已读邮件、本地草稿或粘贴正文：意图、待办、附件。"""
    try:
        if draft_id.strip():
            payload = load_draft(draft_id.strip())
        elif uid.strip():
            box = mailbox.strip() or str(email_settings().get("mailbox") or "INBOX")
            payload = mail_backend().fetch_message(box, uid.strip())
        elif text.strip():
            payload = {
                "uid": "",
                "from": "",
                "to": [],
                "subject": "",
                "body": text,
                "attachments": [],
            }
        else:
            return get_prompt("email_analyze_need_source")
    except (RuntimeError, FileNotFoundError, ValueError, OSError, TimeoutError) as exc:
        return str(exc)
    return _format_analysis(payload)


def email_draft(
    to: str = "",
    subject: str = "",
    body: str = "",
    draft_id: str = "",
    cc: str = "",
    bcc: str = "",
) -> str:
    """新建或改一封本地草稿。未填字段在更新时保持原值。"""
    try:
        if draft_id.strip():
            draft = load_draft(draft_id.strip())
        else:
            draft = {
                "id": f"d-{uuid.uuid4().hex[:8]}",
                "to": [],
                "cc": [],
                "bcc": [],
                "subject": "",
                "body": "",
                "attachments": [],
            }
        if to.strip():
            draft["to"] = _split_addrs(to)
        if cc.strip():
            draft["cc"] = _split_addrs(cc)
        if bcc.strip():
            draft["bcc"] = _split_addrs(bcc)
        if subject:
            draft["subject"] = subject
        if body:
            draft["body"] = body
        saved = save_draft(draft)
    except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
        return str(exc)
    return get_prompt(
        "email_draft_saved",
        draft_id=str(saved["id"]),
        to=", ".join(saved.get("to") or []) or "-",
        subject=str(saved.get("subject") or get_prompt("email_no_subject")),
        attachments=str(len(saved.get("attachments") or [])),
    )


def email_attach(draft_id: str, path: str) -> str:
    """把本地文件挂到草稿附件列表，发送时才读取字节。"""
    try:
        draft = load_draft(draft_id.strip())
        file_path = Path(path).expanduser()
        if not file_path.is_file():
            return get_prompt("email_attach_missing", path=str(file_path))
        resolved = str(file_path.resolve())
        attachments = list(draft.get("attachments") or [])
        if not any(isinstance(item, dict) and item.get("path") == resolved for item in attachments):
            attachments.append({"path": resolved, "name": file_path.name, "size": file_path.stat().st_size})
        draft["attachments"] = attachments
        saved = save_draft(draft)
    except (RuntimeError, FileNotFoundError, ValueError, OSError) as exc:
        return str(exc)
    return get_prompt(
        "email_attach_ok",
        draft_id=str(saved["id"]),
        filename=file_path.name,
        count=str(len(saved.get("attachments") or [])),
    )


def email_send(draft_id: str) -> str:
    """通过已配置 SMTP 发送本地草稿。发出前须审批。"""
    try:
        draft = load_draft(draft_id.strip())
        if not draft.get("to"):
            return get_prompt("email_send_need_to")
        result = mail_backend().send_message(draft)
    except (RuntimeError, FileNotFoundError, ValueError, OSError, TimeoutError) as exc:
        return str(exc)
    return result


def email_save_attachment(uid: str, dest: str, name: str = "", mailbox: str = "") -> str:
    """把邮件附件落到本地路径，不经过公网网盘。"""
    try:
        box = mailbox.strip() or str(email_settings().get("mailbox") or "INBOX")
        row = mail_backend().fetch_message(box, uid.strip())
        wanted = name.strip()
        found: dict[str, Any] | None = None
        attachments = [item for item in (row.get("attachments") or []) if isinstance(item, dict)]
        if wanted:
            found = next((item for item in attachments if str(item.get("name") or "") == wanted), None)
        elif len(attachments) == 1:
            found = attachments[0]
        if found is None:
            return get_prompt("email_attach_not_found", attach_name=wanted or get_prompt("email_unspecified"))
        data = found.get("bytes")
        if not isinstance(data, (bytes, bytearray)):
            return get_prompt("email_attach_no_bytes", attach_name=str(found.get("name") or wanted))
        target = Path(dest).expanduser()
        if dest.endswith(("/", "\\")) or target.is_dir():
            target = target / str(found.get("name") or "attachment")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bytes(data))
    except (RuntimeError, FileNotFoundError, ValueError, OSError, TimeoutError) as exc:
        return str(exc)
    return get_prompt(
        "email_save_ok",
        attach_name=str(found.get("name") or ""),
        dest_path=str(target),
    )


def email_reply(uid: str, body: str, mailbox: str = "", cc: str = "") -> str:
    """按一封已读邮件生成回复草稿，不直接发出。"""
    try:
        box = mailbox.strip() or str(email_settings().get("mailbox") or "INBOX")
        row = mail_backend().fetch_message(box, uid.strip())
        subject = str(row.get("subject") or "")
        if not re.match(r"^(?:re|回复|答复)\s*:", subject, re.IGNORECASE):
            subject = f"Re: {subject}" if subject else "Re:"
        quoted = str(row.get("body") or "")[:2000]
        draft = {
            "to": _split_addrs(str(row.get("from") or "")),
            "cc": _split_addrs(cc) or list(row.get("cc") or []),
            "bcc": [],
            "subject": subject,
            "body": f"{body.strip()}\n\n----\n{quoted}".strip(),
            "attachments": [],
            "in_reply_to": str(row.get("uid") or uid),
        }
        saved = save_draft(draft)
    except (RuntimeError, FileNotFoundError, ValueError, OSError, TimeoutError) as exc:
        return str(exc)
    return get_prompt(
        "email_reply_saved",
        draft_id=str(saved["id"]),
        to=", ".join(saved.get("to") or []) or "-",
        subject=str(saved.get("subject") or get_prompt("email_no_subject")),
    )


def _normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return rows
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            row = {
                "name": str(item.get("name") or "attachment"),
                "size": int(item.get("size") or len(item.get("bytes") or b"")),
            }
            if isinstance(item.get("bytes"), (bytes, bytearray)):
                row["bytes"] = bytes(item["bytes"])
            if item.get("path"):
                row["path"] = str(item["path"])
            rows.append(row)
        elif isinstance(item, str):
            rows.append({"name": item, "size": 0})
    return rows


def _spec(name: str, func: Any, properties: dict[str, Any], required: list[str] | None = None) -> None:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = required
    register_tool(
        ToolSpec(
            name=name,
            description=get_prompt(f"tool_desc_{name}"),
            parameters=parameters,
            func=func,
        )
    )


_spec("mail_status", email_status, {})
_spec(
    "mail_list",
    email_list,
    {
        "mailbox": {"type": "string", "description": get_prompt("email_param_mailbox")},
        "limit": {"type": "integer", "description": get_prompt("email_param_limit")},
        "query": {"type": "string", "description": get_prompt("email_param_query")},
    },
)
_spec(
    "mail_read",
    email_read,
    {
        "uid": {"type": "string", "description": get_prompt("email_param_uid")},
        "mailbox": {"type": "string", "description": get_prompt("email_param_mailbox")},
    },
    ["uid"],
)
_spec(
    "mail_analyze",
    email_analyze,
    {
        "uid": {"type": "string", "description": get_prompt("email_param_uid")},
        "draft_id": {"type": "string", "description": get_prompt("email_param_draft_id")},
        "text": {"type": "string", "description": get_prompt("email_param_text")},
        "mailbox": {"type": "string", "description": get_prompt("email_param_mailbox")},
    },
)
_spec(
    "mail_draft",
    email_draft,
    {
        "to": {"type": "string", "description": get_prompt("email_param_to")},
        "subject": {"type": "string", "description": get_prompt("email_param_subject")},
        "body": {"type": "string", "description": get_prompt("email_param_body")},
        "draft_id": {"type": "string", "description": get_prompt("email_param_draft_id")},
        "cc": {"type": "string", "description": get_prompt("email_param_cc")},
        "bcc": {"type": "string", "description": get_prompt("email_param_bcc")},
    },
)
_spec(
    "mail_attach",
    email_attach,
    {
        "draft_id": {"type": "string", "description": get_prompt("email_param_draft_id")},
        "path": {"type": "string", "description": get_prompt("email_param_path")},
    },
    ["draft_id", "path"],
)
_spec(
    "mail_send",
    email_send,
    {"draft_id": {"type": "string", "description": get_prompt("email_param_draft_id")}},
    ["draft_id"],
)
_spec(
    "mail_save",
    email_save_attachment,
    {
        "uid": {"type": "string", "description": get_prompt("email_param_uid")},
        "dest": {"type": "string", "description": get_prompt("email_param_dest")},
        "name": {"type": "string", "description": get_prompt("email_param_attach_name")},
        "mailbox": {"type": "string", "description": get_prompt("email_param_mailbox")},
    },
    ["uid", "dest"],
)
_spec(
    "mail_reply",
    email_reply,
    {
        "uid": {"type": "string", "description": get_prompt("email_param_uid")},
        "body": {"type": "string", "description": get_prompt("email_param_body")},
        "mailbox": {"type": "string", "description": get_prompt("email_param_mailbox")},
        "cc": {"type": "string", "description": get_prompt("email_param_cc")},
    },
    ["uid", "body"],
)
