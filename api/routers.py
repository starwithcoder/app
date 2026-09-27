from fastapi.routing import APIRouter
from starlette.responses import StreamingResponse

from schemas.request import ChatMessageRequest, SessionMessagesRequest, UserSessionsRequest
from services.agent_service import MultiAgentService
from infrastructure.logging.logger import logger
from services.session_service import session_service

# 1. 定义请求路由器
router = APIRouter()


# 2. 定义对话请求
@router.post("/api/query", summary="智能体对话接口")
async def query(request_context: ChatMessageRequest) -> StreamingResponse:
    """
    SSE返回数据（流式响应）
    响应头中：text/event-stream
    Args:
        request_context: 请求上下文

    Returns:
        StreamingResponse

    """

    # 1. 获取请求上下文的属性
    user_id = request_context.context.user_id
    user_query = request_context.query
    print(request_context.flag)
    logger.info(f"用户 {user_id} 发送的待处理任务 {user_query}")

    # 2. 调用AgentService（智能体的业务服务类）
    async_generator_result = MultiAgentService.process_task(request_context, flag=True)

    # 3. 封装结果到StreamingResponse中
    return StreamingResponse(
        content=async_generator_result,
        status_code=200,
        media_type="text/event-stream"
    )


@router.post("/api/user_sessions")
async def get_user_sessions(request: UserSessionsRequest):
    """
    获取用户的所有会话记忆数据。

    Args:
        request: 包含 user_id 的请求体。

    Returns:
        包含用户所有会话信息和记忆的 JSON 响应。
    """
    # 1. 日志记录：记录请求到达
    logger.info("接收到获取用户会话请求")

    # 2. 参数提取：从请求模型中获取目标用户ID
    user_id = request.user_id
    logger.info(f"获取用户 {user_id} 的所有会话记忆数据")

    try:
        # 3. 服务调用 session_service 从底层存储检索所有历史会话
        all_sessions = await session_service.get_all_sessions_memory(user_id)
        logger.debug(f"成功获取用户 {user_id} 的 {len(all_sessions)} 个会话")

        # 4. 响应构建：组装并返回标准化的成功 JSON 数据
        return {
            "success": True,
            "user_id": user_id,
            "total_sessions": len(all_sessions),
            "sessions": all_sessions
        }
    except Exception as e:
        # 5. 异常处理：捕获服务层抛出的未知错误，记录日志并返回错误标识
        logger.error(f"获取用户 {user_id} 的会话数据时出错: {str(e)}")
        return {
            "success": False,
            "user_id": user_id,
            "error": str(e)
        }


@router.post("/api/session/create", summary="创建新会话")
async def create_session(request: UserSessionsRequest):
    """
    前端点击"新会话"时调用：后端登记会话并返回 session_id。

    前端拿到 session_id 后缓存起来，之后发送消息时带上它即可；
    侧边栏也能立刻显示这个新会话（名字先显示"新对话"，
    等第一条消息进来后自动用首问命名）。

    Args:
        request: 包含 user_id 的请求体。

    Returns:
        {"success": True, "user_id": ..., "session_id": ...}
    """
    user_id = request.user_id
    try:
        session_id = await session_service.create_session(user_id)
        if not session_id:
            return {"success": False, "user_id": user_id, "error": "创建会话失败"}

        logger.info(f"为用户 {user_id} 创建新会话 {session_id}")
        return {
            "success": True,
            "user_id": user_id,
            "session_id": session_id,
        }
    except Exception as e:
        logger.error(f"为用户 {user_id} 创建会话失败: {str(e)}")
        return {
            "success": False,
            "user_id": user_id,
            "error": str(e)
        }


@router.post("/api/session/messages", summary="加载指定会话的消息记录")
async def get_session_messages(request: SessionMessagesRequest):
    """
    前端点开某个会话时调用，加载该会话的聊天记录。

    会话**列表**接口只返回元信息（session_id / title / 条数，不含正文），
    聊天正文走这里按需加载，避免列表请求把全部历史内容都拉回来。

    Args:
        request: 包含 user_id 与 session_id 的请求体。

    Returns:
        {"success": True, "messages": [{"role": ..., "content": ...}, ...]}
    """
    user_id = request.user_id
    session_id = request.session_id
    try:
        messages = await session_service.get_session_messages(user_id, session_id)
        logger.info(f"加载会话 {session_id} 的消息，共 {len(messages)} 条")
        return {
            "success": True,
            "user_id": user_id,
            "session_id": session_id,
            "messages": messages,
        }
    except Exception as e:
        logger.error(f"加载会话 {session_id} 的消息失败: {str(e)}")
        return {
            "success": False,
            "user_id": user_id,
            "session_id": session_id,
            "error": str(e)
        }


@router.post("/api/session/delete", summary="删除会话")
async def delete_session(request: SessionMessagesRequest):
    """
    用户点删除会话。

    **软删除立即生效**：会话马上从列表消失；
    真实清理（补归档 → 导出到分析文件 → Chroma 真删 → 清 Redis）由后台协程异步完成，失败会自动重试。

    Returns:
        {"success": True, "user_id": ..., "session_id": ...}
    """
    user_id = request.user_id
    session_id = request.session_id
    try:
        ok = await session_service.delete_session(user_id, session_id)
        if not ok:
            return {
                "success": False,
                "user_id": user_id,
                "session_id": session_id,
                "error": "标记删除失败",
            }

        logger.info(f"用户 {user_id} 删除会话 {session_id}（已入队）")
        return {"success": True, "user_id": user_id, "session_id": session_id}
    except Exception as e:
        logger.error(f"删除会话 {session_id} 失败: {str(e)}")
        return {
            "success": False,
            "user_id": user_id,
            "session_id": session_id,
            "error": str(e)
        }
