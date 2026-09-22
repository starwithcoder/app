
class SessionRepository:
    """会话数据仓储类（JSON 文件存储）。"""

    # 存储目录名称常量
    STORAGE_DIR_NAME = "user_memories"

    def __init__(self):
        """初始化 SessionRepository，自动定位并创建存储根目录。"""
        current_file = Path(__file__).resolve()
        self._base_dir = current_file.parent.parent
        # 拼接存储路径: backend/app/user_memories
        self._storage_root = self._base_dir / self.STORAGE_DIR_NAME
        # 确保存储根目录存在
        self._storage_root.mkdir(parents=True, exist_ok=True)

    def load_session(self, user_id: str, session_id: str) -> List[Dict[str, Any]]:
        """加载会话消息列表。

        Args:
            user_id: 用户ID。
            session_id: 会话ID。

        Returns:
            List: 会话消息列表（每项为 {role, content} 字典）；
                  文件不存在或解析失败时返回 None。
        """
        #1.从本地仓库拿去会话

        file_path = self._get_file_path(user_id, session_id)
        if not file_path.exists():
            return None
        try:
            with file_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.error(f"加载会话 {file_path} 失败: {e}")
            return None

    def save_session(
            self, user_id: str, session_id: str, chat_history: List[Dict[str, Any]]
    ) -> bool:
        """保存会话消息列表到 JSON 文件。

        Args:
            user_id: 用户ID。
            session_id: 会话ID。
            chat_history: 完整的会话消息列表。

        Returns:
            bool: 是否保存成功。
        """
        try:
            file_path = self._get_file_path(user_id, session_id)
            # 确保用户的个人目录存在（懒加载模式）
            if not file_path.parent.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)

            with file_path.open("w", encoding="utf-8") as f:
                json.dump(chat_history, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存会话 {user_id}/{session_id} 失败: {e}")
            return False

    def get_all_sessions_metadata(
            self, user_id: str
    ) -> List[Tuple[str, str, Union[List, Exception]]]:
        """获取用户所有会话的元数据。

        Args:
            user_id: 用户ID。

        Returns:
            List[Tuple]: 包含 (session_id, create_time, data_or_error) 的列表。
                         data_or_error 为 list 时表示消息数据，为 Exception 时表示读取异常。
        """
        results: List[Tuple[str, str, Union[List, Exception]]] = []
        user_dir = self._get_user_directory(user_id)
        if not user_dir.exists():
            return results
        try:
            for file_path in user_dir.glob("*.json"):
                session_id = file_path.stem
                # 获取文件修改时间作为创建时间
                stat = file_path.stat()
                create_time = datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    results.append((session_id, create_time, data))
                except Exception as e:
                    # 读取或解析失败，返回异常对象（上层隔离处理）
                    results.append((session_id, create_time, e))
        except Exception as e:
            logger.error(f"遍历用户 {user_id} 会话目录失败: {e}")
        return results

    def _get_user_directory(self, user_id: str) -> Path:
        """获取用户的记忆文件夹路径对象。"""
        return self._storage_root / user_id

    def _get_file_path(self, user_id: str, session_id: str) -> Path:
        """获取具体会话文件的路径对象。"""
        return self._get_user_directory(user_id) / f"{session_id}.json"


# 全局单例
session_repository = SessionRepository()