"""
应用配置管理模块

使用 pydantic-settings 进行配置管理，支持：
1. 自动从环境变量读取配置
2. 类型验证和转换
3. 默认值设置
4. 配置文档化
"""
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import model_validator
from typing_extensions import Self


class Settings(BaseSettings):
    """
    应用配置类

    配置项会自动从以下来源读取（优先级从高到低）：
    1. 环境变量
    2. .env 文件
    3. 默认值
    """

    # ==================== AI 服务配置 ====================

    # 硅基流动 API
    SF_API_KEY: Optional[str] = Field(default=None, description="硅基流动 API Key")
    SF_BASE_URL: Optional[str] = Field(default=None, description="硅基流动 Base URL")

    # 阿里百炼 API
    AL_BAILIAN_API_KEY: Optional[str] = Field(default=None, description="阿里百炼 API Key")
    AL_BAILIAN_BASE_URL: Optional[str] = Field(default=None, description="阿里百炼 Base URL")

    # ==================== 模型配置 ====================

    MAIN_MODEL_NAME: Optional[str] = Field(
        default="Qwen/Qwen3-32B",
        description="主模型名称"
    )
    SUB_MODEL_NAME: Optional[str] = Field(
        default="",
        description="qwen3-max"
    )

    # ==================== 数据库配置 ====================

    MYSQL_HOST: Optional[str] = Field(default="localhost", description="MySQL主机地址")
    MYSQL_PORT: int = Field(default=3306, description="MySQL端口")
    MYSQL_USER: Optional[str] = Field(default="root", description="MySQL用户名")
    MYSQL_PASSWORD: Optional[str] = Field(default="", description="MySQL密码")
    MYSQL_DATABASE: Optional[str] = Field(default="its_db", description="MySQL数据库名")
    MYSQL_CHARSET: str = Field(default="utf8mb4", description="MySQL字符集")
    MYSQL_CONNECT_TIMEOUT: int = Field(default=10, description="MySQL连接超时（秒）")
    MYSQL_MAX_CONNECTIONS: int = Field(default=5, description="MySQL最大连接数")

    # ==================== Redis 配置 ====================

    # Redis 连接地址（格式: redis://[:password@]host:port/db）
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis连接地址")
    # 短期记忆过期时间（秒），默认 1 天
    REDIS_SESSION_TTL: int = Field(default=86400, description="短期记忆TTL（秒）")
    # 滚动摘要过期时间（秒），默认 7 天
    # 摘要属于长期记忆，TTL 必须远大于短期记忆，否则用户隔几天回来记忆就断了
    REDIS_SUMMARY_TTL: int = Field(default=7 * 24 * 3600, description="滚动摘要TTL（秒）")
    # 连接超时（秒）
    REDIS_CONNECT_TIMEOUT: int = Field(default=3, description="Redis连接超时（秒）")

    # ==================== 记忆与模型：条数 / 长度上下限 ====================

    # --- ① 用户输入 / 短期记忆 ---
    # 上限：单条消息最大字符数，超出截断（防超长输入撑爆记忆）
    MAX_MESSAGE_CHARS: int = Field(default=4000, description="单条消息最大字符数")
    # 上限：Redis pending 保留条数（scanner 裁剪窗口）
    # 必须 >= SUMMARY_MIN_NEW，否则未摘要的消息会被裁掉
    SHORT_TERM_KEEP: int = Field(default=50, description="短期记忆保留条数")
    # 上限：前端点开会话时加载的历史条数
    HISTORY_LOAD_LIMIT: int = Field(default=50, description="前端加载历史条数")

    # --- ② 摘要 ---
    # 下限：至少新增多少条才触发摘要（避免频繁调 LLM）
    SUMMARY_MIN_NEW: int = Field(default=20, description="触发摘要的最小新增条数")
    # 上限：两次摘要之间最多间隔多少秒（超时强制摘要）
    SUMMARY_MAX_INTERVAL: int = Field(default=1800, description="两次摘要最大间隔（秒）")
    # 上限：摘要最大字数，超出触发"摘要的摘要"压缩
    SUMMARY_MAX_CHARS: int = Field(default=500, description="摘要最大字数")

    # --- ③ 送入模型 ---
    # 下限：至少注入最近多少条（保证上下文连贯）
    CONTEXT_MIN_RECENT: int = Field(default=2, description="注入LLM的最小近期条数")
    # 上限：最多注入最近多少条
    CONTEXT_MAX_RECENT: int = Field(default=20, description="注入LLM的最大近期条数")
    # 上限：本轮上下文 token 预算（超出从旧往新砍）
    CONTEXT_MAX_TOKENS: int = Field(default=6000, description="上下文token预算")

    # 已删除会话的导出目录（删除前会把完整数据存到这里，供后续分析）
    EXPORT_DIR: str = Field(default="exports/deleted", description="已删除会话导出目录")

    # --- ④ 模型输出 ---
    # 上限：单次回复最大 token（在模型调用时限制生成长度）
    MAX_OUTPUT_TOKENS: int = Field(default=2000, description="单次回复最大token")
    # 上限：存入记忆的助手回复最大字符数（入库时截断，兜底防线）
    # 注意：只截断"存档"，用户看到的流式输出始终是完整的
    MAX_REPLY_CHARS: int = Field(default=8000, description="存入记忆的回复最大字符数")

    # ==================== Embedding 配置 ====================

    # ---- 当前方案：硅基流动 API（OpenAI 兼容 /embeddings）----
    # BAAI/bge-m3 = 1024 维，中英多语言，零本地依赖
    #
    # 为什么不用本地 sentence-transformers：需要 torch(~2GB)，且要能连 HuggingFace（当前不可达）
    EMBEDDING_MODEL: str = Field(default="BAAI/bge-m3", description="embedding模型")
    EMBEDDING_API_KEY: Optional[str] = Field(default=None, description="embedding的API Key（不填复用SF_API_KEY）")
    EMBEDDING_BASE_URL: Optional[str] = Field(default=None, description="embedding的Base URL（不填复用SF_BASE_URL）")

    # ---- 备选方案：本地 Ollama（免费、数据不出机器，但占内存）----
    # 切换到 Ollama 只需两步：
    #   1) ollama pull bge-m3          # 约 1.2GB，下载一次即可
    #   2) 把上面三个改成：
    #        EMBEDDING_MODEL     = "bge-m3"
    #        EMBEDDING_BASE_URL  = "http://localhost:11434/v1"
    #        EMBEDDING_API_KEY   = "ollama"      # Ollama 不需要鉴权，但客户端要求非空
    #      并且把下面三个集合名换成 *_v3（换 embedding 来源必须换集合，否则新旧向量混用）
    #   3) 重新迁移：python scripts/migrate_embeddings.py
    #
    # 注意：不要用 qwen2.5:1.5b 这类对话模型做 embedding——Ollama 虽然支持，但检索质量远不如专用 embedding 模型

    # Chroma 集合名
    # 重要：换 embedding 模型后**必须换集合名**——旧向量是旧模型算的，
    #       维度/语义空间都不同，混在一起检索结果无意义
    MEMORIES_COLLECTION: str = Field(default="memories_v2", description="会话记忆集合名")
    SUMMARY_COLLECTION: str = Field(default="summary_v2", description="摘要集合名")
    FACTS_COLLECTION: str = Field(default="facts_v2", description="原子事实集合名")

    # ==================== 外部服务配置 ====================

    # 知识库服务
    KNOWLEDGE_BASE_URL: Optional[str] = Field(
        default=None,
        description="知识库服务URL"
    )

    # 通义千问搜索服务
    DASHSCOPE_BASE_URL: Optional[str] = Field(
        default=None,
        description="通义千问 DashScope Base URL"
    )
    DASHSCOPE_API_KEY: Optional[str] = Field(
        default=None,
        description="通义千问 DashScope API Key（从 .env 读取，勿硬编码）"
    )

    # 百度地图服务
    BAIDUMAP_AK: Optional[str] = Field(
        default=None,
        description="百度地图 AK (Access Key)"
    )

    # ==================== Pydantic Settings 配置 ====================

    model_config = SettingsConfigDict(
        # 计算.env文件的绝对路径：config目录的父目录(app目录)下的.env
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",          # .env文件编码
        case_sensitive=True,                 # 环境变量名大小写敏感
        extra="ignore",                      # 忽略额外的环境变量
        validate_default=True,               # 验证默认值
    )

    # ====================  ====================
    @model_validator(mode='after')
    def check_ai_service_configuration(self) -> Self:
        """
        验证器：在配置加载完成后自动执行。
        如果需要强制至少配置一个 AI 服务，可以在这里抛出 ValueError
        """
        # 注意：这里 self 已经是实例化后的模型对象
        has_service = any([
            self.SF_API_KEY and self.SF_BASE_URL,
            self.AL_BAILIAN_API_KEY and self.AL_BAILIAN_BASE_URL
        ])

        if not has_service:
            raise ValueError("必须配置至少一个 AI 服务 (硅基流动 或 阿里百炼)")

        return self

    # ====================定时扫描 配置====================
    SCAN_INTERVAL:float | None = Field(default=60,description="")
    SESSION_TIMEOUT:float |None = Field(default=300,description="")

   

# 创建全局配置实例
settings = Settings()

