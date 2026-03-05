from __future__ import annotations

import json
import mimetypes
import re
import shutil
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

PERMISSIONS_FILE = ".workspace_permissions.json"


def _load_permissions(skills_dir: Path) -> dict[str, list[str]]:
    """
    Read .workspace_permissions.json from skills_dir.
    Format: { "skill_folder": ["workspace_id_1", ...], ... }
    Skills absent = public. Skills present = restricted to listed workspaces.
    """
    perm_path = skills_dir / PERMISSIONS_FILE
    if not perm_path.is_file():
        return {}
    try:
        data = json.loads(perm_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, list)}
    except Exception:
        return {}


def _save_permissions(skills_dir: Path, perms: dict[str, list[str]]) -> None:
    perm_path = skills_dir / PERMISSIONS_FILE
    perm_path.write_text(json.dumps(perms, ensure_ascii=False, indent=2), encoding="utf-8")


def get_file_content(url: str, timeout: int = 30) -> bytes:
    try:
        req = Request(url, headers={"User-Agent": "dify-plugin-skill/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise RuntimeError(f"文件下载失败: {str(e)}") from e


def get_skills_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def list_skills_sorted() -> list[Path]:
    skills_dir = get_skills_dir()
    folders = [p for p in skills_dir.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.stat().st_ctime)
    return folders


def extract_url_and_name(file_item: Any) -> tuple[str | None, str | None]:
    url = None
    name = None
    if hasattr(file_item, "url"):
        url = getattr(file_item, "url", None)
    if hasattr(file_item, "filename"):
        name = getattr(file_item, "filename", None)
    if hasattr(file_item, "name") and not name:
        name = getattr(file_item, "name", None)
    if isinstance(file_item, dict):
        url = file_item.get("url", url)
        name = file_item.get("filename", name) or file_item.get("name", name)
    return url, name


def infer_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix
    return ext if ext else ".zip"


def safe_filename(preferred_name: str | None, fallback_ext: str = ".zip") -> str:
    if preferred_name:
        base = Path(preferred_name).name
        base = re.sub(r"[<>:\"/\\\\|?*]+", "_", base).strip()
        if base:
            return base
    return f"{uuid.uuid4().hex}{fallback_ext}"


def _is_within_dir(base: Path, target: Path) -> bool:
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        return base_resolved == target_resolved or base_resolved in target_resolved.parents
    except Exception:
        return False


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name:
                continue
            if name.startswith("/") or name.startswith("\\"):
                raise RuntimeError("压缩包包含非法路径")
            target_path = (dest_dir / name).resolve()
            if not _is_within_dir(dest_dir, target_path):
                raise RuntimeError("压缩包包含越权路径")
            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_skill_folders(extracted_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for p in extracted_root.iterdir():
        if p.is_dir():
            candidates.append(p)
    if candidates:
        with_skill_md = [p for p in candidates if (p / "SKILL.md").is_file()]
        if with_skill_md:
            return with_skill_md
        if len(candidates) == 1:
            return candidates
        return candidates
    if (extracted_root / "SKILL.md").is_file():
        return [extracted_root]
    return []


class TMTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        command = str(tool_parameters.get(“command”, “”)).strip()
        files_param = tool_parameters.get(“files”)
        workspace_id = str(tool_parameters.get(“workspace_id”) or “”).strip() or None

        if command in (“查看技能”, “查看 技能”, “查看”):
            skills = list_skills_sorted()
            if not skills:
                yield self.create_text_message(“❌当前没有已存入的技能包。\n”)
                return
            skills_dir = get_skills_dir()
            perms = _load_permissions(skills_dir)
            lines: list[str] = []
            for idx, p in enumerate(skills):
                allowed = perms.get(p.name)
                if allowed is None:
                    scope = “（公开）”
                elif allowed:
                    scope = f”（空间：{', '.join(allowed)}）”
                else:
                    scope = “（私有，未授权任何空间）”
                lines.append(f”{idx + 1}. {p.name} {scope}”)
            yield self.create_text_message(“\n”.join(lines))
            return

        if command in (“新增技能”, “存入技能”, “保存技能”):
            file_items: list[Any] = []
            if isinstance(files_param, list):
                file_items = [x for x in files_param if x]
            elif files_param:
                file_items = [files_param]
            elif “file” in tool_parameters and tool_parameters[“file”]:
                file_items = [tool_parameters[“file”]]

            if not file_items:
                yield self.create_text_message(“❌未检测到上传的 zip 文件，请提供 files 参数。\n”)
                return

            skills_dir = get_skills_dir()
            installed: list[str] = []

            for file_item in file_items:
                url, preferred_name = extract_url_and_name(file_item)
                if not url:
                    yield self.create_text_message(“❌无法获取文件URL，请检查入参（files[i].url）。\n”)
                    return

                filename_attr = None
                try:
                    filename_attr = getattr(file_item, “filename”, None)
                except Exception:
                    filename_attr = None
                if isinstance(file_item, dict):
                    filename_attr = file_item.get(“filename”, filename_attr)

                try:
                    content = get_file_content(url)
                except Exception as e:
                    yield self.create_text_message(str(e))
                    return

                if filename_attr:
                    filename = Path(filename_attr).name
                else:
                    ext = infer_ext_from_url(url)
                    filename = safe_filename(preferred_name, fallback_ext=ext if ext else “.zip”)

                with tempfile.TemporaryDirectory(prefix=”skill-upload-”) as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / filename
                    try:
                        zip_path.write_bytes(content)
                    except Exception as e:
                        yield self.create_text_message(f”❌保存临时文件失败：{e}\n”)
                        return

                    extract_dir = tmp_dir / “extracted”
                    try:
                        _safe_extract_zip(zip_path, extract_dir)
                    except Exception as e:
                        yield self.create_text_message(f”❌解压失败：{e}\n”)
                        return

                    skill_folders = _find_skill_folders(extract_dir)
                    if not skill_folders:
                        yield self.create_text_message(“❌压缩包内未找到技能目录（应包含 SKILL.md）。\n”)
                        return

                    for folder in skill_folders:
                        target = skills_dir / folder.name
                        if target.exists():
                            yield self.create_text_message(f”❌技能已存在：{folder.name}（请先删除同名技能）\n”)
                            return
                        try:
                            shutil.move(str(folder), str(target))
                            installed.append(target.name)
                        except Exception as e:
                            yield self.create_text_message(f”❌安装技能失败：{e}\n”)
                            return

            # If workspace_id provided, register the installed skills as private to that workspace
            if workspace_id and installed:
                perms = _load_permissions(skills_dir)
                for skill_folder in installed:
                    existing = perms.get(skill_folder, [])
                    if workspace_id not in existing:
                        existing = existing + [workspace_id]
                    perms[skill_folder] = existing
                _save_permissions(skills_dir, perms)
                scope_note = f”（已限定空间：{workspace_id}）”
            else:
                scope_note = “（公开，所有空间可用）”

            yield self.create_text_message(“✅技能已安装：\n” + “\n”.join(installed) + f”\n{scope_note}\n”)
            skills = list_skills_sorted()
            lines = [f”{idx + 1}. {p.name}” for idx, p in enumerate(skills)]
            yield self.create_text_message(“👓当前技能列表：\n” + (“\n”.join(lines) if lines else “（空）\n”))
            return

        m_del = re.match(r”^删除技能(\d+)$”, command)
        if m_del:
            idx = int(m_del.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message(“❌技能序号无效或超出范围。请先使用”查看技能”确认序号。\n”)
                return
            target = skills[idx - 1]
            skill_name = target.name
            try:
                shutil.rmtree(target, ignore_errors=False)
            except Exception as e:
                yield self.create_text_message(f”❌删除失败：{e}\n”)
                return
            # Also clean up permissions entry for this skill
            skills_dir = get_skills_dir()
            perms = _load_permissions(skills_dir)
            if skill_name in perms:
                del perms[skill_name]
                _save_permissions(skills_dir, perms)
            yield self.create_text_message(f”✅已删除技能{idx}：{skill_name}\n”)
            skills = list_skills_sorted()
            if not skills:
                yield self.create_text_message(“😑当前技能列表为空。\n”)
            else:
                lines = [f”{i + 1}. {p.name}” for i, p in enumerate(skills)]
                yield self.create_text_message(“👓当前技能列表：\n” + “\n”.join(lines))
            return

        m_dl = re.match(r”^下载技能(\d+)$”, command)
        if m_dl:
            idx = int(m_dl.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message(“❌技能序号无效或超出范围。请先使用”查看技能”确认序号。\n”)
                return
            target = skills[idx - 1]

            try:
                with tempfile.TemporaryDirectory(prefix=”skill-zip-”) as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / f”{target.name}.zip”
                    shutil.make_archive(str(zip_path.with_suffix(“”)), “zip”, root_dir=target.parent, base_dir=target.name)
                    blob = zip_path.read_bytes()
            except Exception as e:
                yield self.create_text_message(f”❌读取文件失败：{e}\n”)
                return

            mime_type, _ = mimetypes.guess_type(f”{target.name}.zip”)
            if not mime_type:
                mime_type = “application/zip”

            yield self.create_text_message(f”⬇️开始下载技能{idx}：{target.name}.zip\n”)
            yield self.create_blob_message(
                blob=blob,
                meta={
                    “mime_type”: mime_type,
                    “filename”: f”{target.name}.zip”,
                },
            )
            return

        # 查看空间权限 - show all permission assignments
        if command in (“查看空间权限”, “查看权限”):
            skills_dir = get_skills_dir()
            perms = _load_permissions(skills_dir)
            if not perms:
                yield self.create_text_message(“当前所有技能均为公开状态（无空间权限限制）。\n”)
                return
            lines = [“[空间权限配置]”]
            for skill_folder, wids in sorted(perms.items()):
                if wids:
                    lines.append(f”  {skill_folder} → {', '.join(wids)}”)
                else:
                    lines.append(f”  {skill_folder} → （未授权任何空间）”)
            yield self.create_text_message(“\n”.join(lines) + “\n”)
            return

        # 授权技能N - grant workspace_id access to skill N
        m_grant = re.match(r”^授权技能(\d+)$”, command)
        if m_grant:
            if not workspace_id:
                yield self.create_text_message(“❌请在 workspace_id 字段填写要授权的空间 ID。\n”)
                return
            idx = int(m_grant.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message(“❌技能序号无效或超出范围。请先使用”查看技能”确认序号。\n”)
                return
            skill_folder = skills[idx - 1].name
            skills_dir = get_skills_dir()
            perms = _load_permissions(skills_dir)
            existing = perms.get(skill_folder, [])
            if workspace_id in existing:
                yield self.create_text_message(f”⚠️ 空间「{workspace_id}」已有技能「{skill_folder}」的权限，无需重复授权。\n”)
                return
            perms[skill_folder] = existing + [workspace_id]
            _save_permissions(skills_dir, perms)
            yield self.create_text_message(f”✅已将技能「{skill_folder}」授权给空间「{workspace_id}」。\n”)
            return

        # 撤销授权N - revoke workspace_id access from skill N
        m_revoke = re.match(r”^撤销授权(\d+)$”, command)
        if m_revoke:
            if not workspace_id:
                yield self.create_text_message(“❌请在 workspace_id 字段填写要撤销授权的空间 ID。\n”)
                return
            idx = int(m_revoke.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message(“❌技能序号无效或超出范围。请先使用”查看技能”确认序号。\n”)
                return
            skill_folder = skills[idx - 1].name
            skills_dir = get_skills_dir()
            perms = _load_permissions(skills_dir)
            existing = perms.get(skill_folder, [])
            if workspace_id not in existing:
                yield self.create_text_message(f”⚠️ 空间「{workspace_id}」本就没有技能「{skill_folder}」的权限。\n”)
                return
            perms[skill_folder] = [w for w in existing if w != workspace_id]
            _save_permissions(skills_dir, perms)
            yield self.create_text_message(f”✅已撤销空间「{workspace_id}」对技能「{skill_folder}」的访问权限。\n”)
            return

        yield self.create_text_message(
            “😑未识别的技能管理命令。支持：\n”
            “  查看技能 / 新增技能 / 删除技能N / 下载技能N\n”
            “  查看空间权限 / 授权技能N / 撤销授权N\n”
        )
        return
