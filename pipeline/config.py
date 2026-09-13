"""配置加载:唯一的配置入口,其余模块只从这里拿参数。"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class CollectConfig:
    keywords: list[str]
    request_interval: int = 5
    daily_limit: int = 200
    max_pages_per_keyword: int = 2
    max_retries: int = 3
    cookie: str = ""


@dataclass
class FilterConfig:
    interview_words: list[str] = field(default_factory=list)
    position_words: list[str] = field(default_factory=list)


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    daily_token_budget: int = 500000
    chunk_chars: int = 6000


@dataclass
class OutputConfig:
    raw_dir: str = "raw"
    db_path: str = "data/interviews.db"
    output_dir: str = "output"
    pending_dir: str = "output/pending"
    template_dir: str = "templates"
    log_dir: str = "logs"


@dataclass
class AppConfig:
    collect: CollectConfig
    filter: FilterConfig
    llm: LLMConfig
    output: OutputConfig
    base_dir: Path = PROJECT_ROOT

    def resolve(self, relative: str) -> Path:
        """把配置中的相对路径解析为基于项目根目录的绝对路径。"""
        return (self.base_dir / relative).resolve()


def load_config(path: str | Path | None = None) -> AppConfig:
    """读取 config.yaml;文件缺失或字段缺失时使用保守默认值。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        logger.warning("配置文件不存在: %s,使用默认配置", cfg_path)

    collect = CollectConfig(**(data.get("collect") or {}))
    filt = FilterConfig(**(data.get("filter") or {}))
    llm = LLMConfig(**(data.get("llm") or {}))
    output = OutputConfig(**(data.get("output") or {}))
    return AppConfig(collect=collect, filter=filt, llm=llm, output=output)


def setup_logging(log_dir: Path, verbose: bool = False) -> None:
    """日志同时输出到控制台与 logs/ 目录(UTF-8)。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.FileHandler(
        log_dir / "pipeline.log", encoding="utf-8", mode="a"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
