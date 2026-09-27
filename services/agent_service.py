import re
from collections.abc import AsyncGenerator
from agents.run import Runner, RunConfig
from multi_agent.memory_tools import SessionContext
from multi_agent.orchestrator_agent import orchestrator_agent
from schemas.request import ChatMessageRequest
from services.session_service import session_service
from services.stream_response_service import process_stream_response
from utils.response_util import ResponseFactory
from infrastructure.logging.logger import logger
import traceback
from schemas.response import ContentKind


class MultiAgentService:
    """
    多智能体业务服务类
    todo:
    process_task:方法前面加上async 以及返回值类型一定是AsyncGenerator
    """

    @classmethod
    async def process_task(cls, request: ChatMessageRequest, flag: bool) -> AsyncGenerator:
        """
        多智能体处理任务入口
        Args:
            request:  请求上下文

        Returns:
            AsyncGenerator：异步生成器对象（必须）
        """
        try:
            # 1. 获取请求上下文的信息
            user_id = request.context.user_id
            session_id = request.context.session_id
            user_query = request.query

            # 2. 准备历史对话（装配短期记忆上下文，只读）
            chat_history = await session_service.prepare_history(user_id, session_id, user_query)

            # 3. 先把用户消息落库（不等模型答完）
            #    好处：流式中断 / 客户端断开也不会丢这一轮，
            #    且会话立刻进入持久索引（含首问自动命名），侧边栏马上可见
            #    注意：只在首次执行时记录，重试（flag=False）不再重复写
            if flag:
                await session_service.record_user_message(user_id, session_id, user_query)

            # 4. 运行Agent
            #    注入会话上下文：记忆工具靠它取 user_id / session_id
            #    （不注入的话，模型调 recall 时不知道该查哪个用户）
            streaming_result = Runner.run_streamed(
                starting_agent=orchestrator_agent,
                input=chat_history,  # 列表
                context=SessionContext(
                    user_id=user_id,
                    session_id=session_id or "default_session",
                ),
                max_turns=5,  # COT(思考 行动 观察)--->迭代多少次（不是异常重试）
                run_config=RunConfig(tracing_disabled=True)
            )

            # 5. 处理Agent的事件流（事件流）
            async for chunk in process_stream_response(streaming_result):
                yield chunk

            # 6. 获取Agent的结果
            agent_result = streaming_result.final_output

            format_agent_result = re.sub(r'\n+', '\n', agent_result)
            # 7. 只补存助手回复（用户消息已在第 3 步落库）
            await session_service.record_assistant_message(
                user_id, session_id, format_agent_result
            )
        except Exception as e:
            # 记录错误日志
            logger.error(f"AgentService.process_query执行出错: {str(e)}")
            logger.debug(f"异常详情: {traceback.format_exc()}")

            text = f"❌ 系统错误: {str(e)}"
            yield "data: " + ResponseFactory.build_text(
                text, ContentKind.PROCESS
            ).model_dump_json() + "\n\n"

            # 如果允许重试，则启动重试流程
            if flag:
                text = f"🔄 正在尝试自动重试..."
                yield "data: " + ResponseFactory.build_text(
                    text, ContentKind.PROCESS
                ).model_dump_json() + "\n\n"

                # 递归调用进行重试
                async for item in MultiAgentService.process_task(request,flag=False):
                    yield item
