from __future__ import annotations

import hashlib
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


def get_file_content(url: str, timeout: int = 30) -> bytes:
    try:
        req = Request(url, headers={"User-Agent": "dify-plugin-skill/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise RuntimeError(f"文件下载失败: {str(e)}") from e


def _project_root(project_name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "skills" / project_name


def get_skills_dir(project_name: str) -> Path:
    skills_dir = _project_root(project_name)
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def _token_file(project_name: str) -> Path:
    return _project_root(project_name) / ".token"


def _project_exists(project_name: str) -> bool:
    return _project_root(project_name).is_dir()


def _is_locked(project_name: str) -> bool:
    return _token_file(project_name).exists()


def _save_token(project_name: str, token: str) -> None:
    h = hashlib.sha256(token.encode()).hexdigest()
    _token_file(project_name).write_text(h)


def _check_token(project_name: str, token: str) -> bool:
    tf = _token_file(project_name)
    if not tf.exists():
        return True  # 未设置令牌，开放访问
    if not token:
        return False
    return tf.read_text().strip() == hashlib.sha256(token.encode()).hexdigest()


def normalize_project_name(raw: str) -> tuple[str, str | None]:
    """Normalize project name and return (normalized, warning_if_changed)."""
    normalized = raw.strip().lower()
    normalized = re.sub(r"[^a-z0-9_\-\u4e00-\u9fff]", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized:
        return "", "项目名称不能为空或仅含特殊字符"
    warning = f"项目名称已规范化：「{raw}」→「{normalized}」" if normalized != raw else None
    return normalized, warning


def list_projects() -> list[Path]:
    root = Path(__file__).resolve().parent.parent
    skills_root = root / "skills"
    if not skills_root.exists():
        return []
    return sorted([p for p in skills_root.iterdir() if p.is_dir()], key=lambda p: p.stat().st_ctime)


def list_skills_sorted(project_name: str) -> list[Path]:
    skills_dir = get_skills_dir(project_name)
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
        command = str(tool_parameters.get("command", "")).strip()
        files_param = tool_parameters.get("files")
        raw_project = str(tool_parameters.get("project_name") or "").strip()
        project_name, norm_warning = normalize_project_name(raw_project)
        if not project_name:
            yield self.create_text_message("❌请填写项目名称（project_name）。\n")
            return
        if norm_warning:
            yield self.create_text_message(f"⚠️ {norm_warning}\n")
        project_token = str(tool_parameters.get("project_token") or "").strip()

        if command in ("查看项目", "项目列表", "列出项目"):
            projects = list_projects()
            if not projects:
                yield self.create_text_message("📭 当前还没有任何项目。\n")
                return
            lines = [f"{i + 1}. {p.name}" for i, p in enumerate(projects)]
            yield self.create_text_message("📂 已有项目列表：\n" + "\n".join(lines) + "\n")
            return

        if command in ("查看技能", "查看 技能", "查看"):
            skills = list_skills_sorted(project_name)
            if not skills:
                yield self.create_text_message(f"❌项目【{project_name}】当前没有已存入的技能包。\n")
                return
            lines = [f"{idx + 1}. {p.name}" for idx, p in enumerate(skills)]
            yield self.create_text_message("\n".join(lines))
            return

        if command in ("新增技能", "存入技能", "保存技能"):
            # 令牌校验：项目已存在且已设置令牌时必须验证
            if _project_exists(project_name) and _is_locked(project_name):
                if not _check_token(project_name, project_token):
                    yield self.create_text_message(
                        f"🔒 项目【{project_name}】已设置访问令牌，project_token 不正确，拒绝写入。\n"
                    )
                    return

            file_items: list[Any] = []
            if isinstance(files_param, list):
                file_items = [x for x in files_param if x]
            elif files_param:
                file_items = [files_param]
            elif "file" in tool_parameters and tool_parameters["file"]:
                file_items = [tool_parameters["file"]]

            if not file_items:
                yield self.create_text_message("❌未检测到上传的 zip 文件，请提供 files 参数。\n")
                return

            is_new_project = not _project_exists(project_name)
            skills_dir = get_skills_dir(project_name)
            # 新项目：如果提供了令牌则锁定
            if is_new_project and project_token:
                _save_token(project_name, project_token)
                yield self.create_text_message(
                    f"🔐 项目【{project_name}】已创建并设置访问令牌，请妥善保管令牌，后续写操作需要提供。\n"
                )
            installed: list[str] = []

            for file_item in file_items:
                # 优先用 SDK 提供的 blob，避免走 HTTP（内部 URL 需要认证会 502）
                content: bytes | None = None
                if hasattr(file_item, "blob") and file_item.blob is not None:
                    content = bytes(file_item.blob)

                url, preferred_name = extract_url_and_name(file_item)

                if content is None:
                    if not url:
                        yield self.create_text_message("❌无法获取文件内容，请检查入参（files[i]）。\n")
                        return
                    try:
                        content = get_file_content(url)
                    except Exception as e:
                        yield self.create_text_message(str(e))
                        return

                filename_attr = None
                try:
                    filename_attr = getattr(file_item, "filename", None)
                except Exception:
                    pass
                if isinstance(file_item, dict):
                    filename_attr = file_item.get("filename", filename_attr)

                if filename_attr:
                    filename = Path(filename_attr).name
                else:
                    ext = infer_ext_from_url(url) if url else ".zip"
                    filename = safe_filename(preferred_name, fallback_ext=ext if ext else ".zip")

                with tempfile.TemporaryDirectory(prefix="skill-upload-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / filename
                    try:
                        zip_path.write_bytes(content)
                    except Exception as e:
                        yield self.create_text_message(f"❌保存临时文件失败：{e}\n")
                        return

                    extract_dir = tmp_dir / "extracted"
                    try:
                        _safe_extract_zip(zip_path, extract_dir)
                    except Exception as e:
                        yield self.create_text_message(f"❌解压失败：{e}\n")
                        return

                    skill_folders = _find_skill_folders(extract_dir)
                    if not skill_folders:
                        yield self.create_text_message("❌压缩包内未找到技能目录（应包含 SKILL.md）。\n")
                        return

                    for folder in skill_folders:
                        target = skills_dir / folder.name
                        if target.exists():
                            yield self.create_text_message(f"❌技能已存在：{folder.name}（请先删除同名技能）\n")
                            return
                        try:
                            shutil.move(str(folder), str(target))
                            installed.append(target.name)
                        except Exception as e:
                            yield self.create_text_message(f"❌安装技能失败：{e}\n")
                            return

            yield self.create_text_message("✅技能已安装：\n" + "\n".join(installed) + "\n")
            skills = list_skills_sorted(project_name)
            lines = [f"{idx + 1}. {p.name}" for idx, p in enumerate(skills)]
            yield self.create_text_message("👓当前技能列表：\n" + ("\n".join(lines) if lines else "（空）\n"))
            return

        m_del = re.match(r"^删除技能(\d+)$", command)
        if m_del:
            if _is_locked(project_name) and not _check_token(project_name, project_token):
                yield self.create_text_message(
                    f"🔒 项目【{project_name}】已设置访问令牌，project_token 不正确，拒绝删除。\n"
                )
                return
            idx = int(m_del.group(1))
            skills = list_skills_sorted(project_name)
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]
            try:
                shutil.rmtree(target, ignore_errors=False)
            except Exception as e:
                yield self.create_text_message(f"❌删除失败：{e}\n")
                return
            yield self.create_text_message(f"✅已删除技能{idx}：{target.name}\n")
            skills = list_skills_sorted(project_name)
            if not skills:
                yield self.create_text_message("😑当前技能列表为空。\n")
            else:
                lines = [f"{i + 1}. {p.name}" for i, p in enumerate(skills)]
                yield self.create_text_message("👓当前技能列表：\n" + "\n".join(lines))
            return

        m_dl = re.match(r"^下载技能(\d+)$", command)
        if m_dl:
            idx = int(m_dl.group(1))
            skills = list_skills_sorted(project_name)
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]

            try:
                with tempfile.TemporaryDirectory(prefix="skill-zip-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / f"{target.name}.zip"
                    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=target.parent, base_dir=target.name)
                    blob = zip_path.read_bytes()
            except Exception as e:
                yield self.create_text_message(f"❌读取文件失败：{e}\n")
                return

            mime_type, _ = mimetypes.guess_type(f"{target.name}.zip")
            if not mime_type:
                mime_type = "application/zip"

            yield self.create_text_message(f"⬇️开始下载技能{idx}：{target.name}.zip\n")
            yield self.create_blob_message(
                blob=blob,
                meta={
                    "mime_type": mime_type,
                    "filename": f"{target.name}.zip",
                },
            )
            return

        yield self.create_text_message("😑未识别的技能管理命令。支持：查看技能、新增技能、删除技能N、下载技能N。\n")
        return
