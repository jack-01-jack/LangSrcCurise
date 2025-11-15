# -*- coding: utf-8 -*-
"""Integration helpers for the OneForAll subdomain enumeration toolkit."""

from __future__ import annotations

import configparser
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Set


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.ini"


class OneForAllRunner:
    """Wrapper around the external OneForAll tool.

    The integration keeps the original project optional: if the executable is
    missing or disabled in the configuration, the runner simply returns an empty
    result set instead of raising errors. This prevents the main scheduler from
    crashing when OneForAll is not available while still allowing operators to
    benefit from richer subdomain data when it is installed.
    """

    def __init__(self) -> None:
        self._enabled = True
        self._workspace: Path | None = None
        self._script: Path | None = None
        self._results_dir: Path | None = None
        self._python_bin: str = sys.executable
        self._timeout: int = 900
        self._extra_args: List[str] = []
        self._load_config()

    # ------------------------------------------------------------------
    # public helpers
    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._enabled and self._script is not None and self._script.exists()

    def enumerate(self, domain: str) -> List[str]:
        if not self.available:
            return []

        initial_files = self._collect_existing_results()
        temp_results: Path | None = None
        try:
            temp_results = self._invoke(domain)
        except subprocess.SubprocessError as exc:
            print(f"[OneForAll] 枚举 {domain} 失败: {exc}")
            return []

        candidate_files: List[Path] = []
        if temp_results is not None:
            candidate_files.extend(sorted(temp_results.glob("*.json")))

        if not candidate_files:
            candidate_files.extend(self._collect_new_results(initial_files))

        if not candidate_files:
            if temp_results is not None:
                shutil.rmtree(temp_results, ignore_errors=True)
            return []

        subdomains: Set[str] = set()
        for file_path in candidate_files:
            subdomains.update(self._parse_result_file(file_path))

        results = sorted(self._normalise_results(domain, subdomains))
        if temp_results is not None:
            shutil.rmtree(temp_results, ignore_errors=True)
        return results

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        cfg = configparser.ConfigParser()
        cfg.read(str(CONFIG_PATH))

        if cfg.has_section("OneForAll"):
            self._enabled = cfg.getboolean("OneForAll", "enabled", fallback=True)
            workspace = cfg.get("OneForAll", "workspace", fallback="ExtrApps/OneForAll") or "ExtrApps/OneForAll"
            results_dir = cfg.get("OneForAll", "results_dir", fallback="results") or "results"
            python_value = cfg.get("OneForAll", "python", fallback=sys.executable)
            self._python_bin = python_value or sys.executable
            self._timeout = cfg.getint("OneForAll", "timeout", fallback=900)
            extra_args = cfg.get("OneForAll", "extra_args", fallback="")
            if extra_args:
                self._extra_args = shlex.split(extra_args)
            self._workspace = (BASE_DIR / workspace).resolve()
            self._results_dir = (self._workspace / results_dir).resolve()
        else:
            self._workspace = (BASE_DIR / "ExtrApps" / "OneForAll").resolve()
            self._results_dir = (self._workspace / "results").resolve()

        script = self._workspace / "oneforall.py" if self._workspace else None
        if not self._enabled or script is None or not script.exists():
            if self._enabled:
                print("[OneForAll] 未找到 oneforall.py，已禁用该集成。")
            self._enabled = False
            self._script = None
        else:
            self._script = script

    def _invoke(self, domain: str) -> Path | None:
        assert self._script is not None
        base_command = [self._python_bin, str(self._script), "--target", domain, "--format", "json"]

        env = os.environ.copy()
        existing_path = env.get("PYTHONPATH", "")
        workspace_path = str(self._workspace) if self._workspace else ""
        if workspace_path:
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [workspace_path, existing_path]))

        cwd = str(self._workspace) if self._workspace else None

        def run_command(cmd: List[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                check=False,
                text=True,
            )

        temp_dir: Path | None = None
        command = base_command.copy()
        if self._extra_args:
            command.extend(self._extra_args)

        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="oneforall-"))
            command_with_path = command + ["--path", str(temp_dir)]
            completed = run_command(command_with_path)
        except Exception:
            temp_dir = None
            completed = run_command(command)

        if completed.returncode != 0 and temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            temp_dir = None
            completed = run_command(command)

        if completed.returncode != 0:
            print(
                f"[OneForAll] 执行命令失败 (code {completed.returncode}): {completed.stderr.strip() or completed.stdout.strip()}"
            )
            raise subprocess.SubprocessError("OneForAll execution failed")

        return temp_dir

    def _collect_existing_results(self) -> Set[Path]:
        if self._results_dir and self._results_dir.exists():
            return {path for path in self._results_dir.glob("*.json")}
        return set()

    def _collect_new_results(self, existing: Set[Path]) -> List[Path]:
        if not self._results_dir or not self._results_dir.exists():
            return []
        current = {path for path in self._results_dir.glob("*.json")}
        new_files = [path for path in current if path not in existing]
        new_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return new_files

    @staticmethod
    def _parse_result_file(file_path: Path) -> Set[str]:
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:  # pragma: no cover - defensive coding
            print(f"[OneForAll] 无法解析结果文件 {file_path}: {exc}")
            return set()

        items: Iterable = []
        if isinstance(data, dict):
            for key in ("results", "data", "domains", "subdomains"):
                if key in data:
                    value = data[key]
                    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                        items = value
                        break
            if not items:
                items = data.values()
        elif isinstance(data, list):
            items = data

        results: Set[str] = set()
        for item in items:
            if isinstance(item, dict):
                candidate = item.get("subdomain") or item.get("domain") or item.get("url")
                if not candidate:
                    continue
                results.add(str(candidate).strip())
            elif isinstance(item, str):
                results.add(item.strip())

        return results

    @staticmethod
    def _normalise_results(domain: str, records: Set[str]) -> Set[str]:
        normalised: Set[str] = set()
        for record in records:
            if not record:
                continue
            value = record if record.startswith("http") else f"http://{record}"
            if domain in value:
                normalised.add(value)
        return normalised


_RUNNER = OneForAllRunner()


def is_oneforall_available() -> bool:
    return _RUNNER.available


def collect_from_oneforall(domain: str) -> List[str]:
    return _RUNNER.enumerate(domain)

