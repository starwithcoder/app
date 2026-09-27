import { ref } from 'vue';

const API_URL = 'http://127.0.0.1:8000/api/user_sessions';
const CREATE_SESSION_URL = 'http://127.0.0.1:8000/api/session/create';
const MESSAGES_URL = 'http://127.0.0.1:8000/api/session/messages';
const DELETE_SESSION_URL = 'http://127.0.0.1:8000/api/session/delete';

/** 格式化成本地时间 'YYYY-MM-DD HH:MM:SS'，与后端返回格式保持一致 */
const formatTime = (date) => {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
};

/**
 * 历史会话列表
 * @param {object} options
 * @param {import('vue').Ref<string>} options.currentUser 当前登录用户名
 * @param {(session: object|null) => void} [options.onSessionChange] 选中会话变化时通知外部加载会话内容
 * @param {() => void} [options.onLoaded] 列表刷新完成后回调（用于滚动到底部）
 */
export function useSessions({ currentUser, onSessionChange, onLoaded }) {
  const sessions = ref([]);
  const selectedSessionId = ref('');
  const isLoadingSessions = ref(false);
  const showSessions = ref(true);

  const toggleSessions = () => {
    showSessions.value = !showSessions.value;
  };

  const selectSession = (sessionId) => {
    selectedSessionId.value = sessionId;
    const session = sessions.value.find(s => s.session_id === sessionId) || null;
    onSessionChange?.(session);
  };

  /**
   * 新建会话：交给**后端**创建并登记，使用后端返回的 session_id。
   *
   * 之前是纯前端本地生成 id，后端完全不知道；
   * 一刷新（fetchSessions 从后端拉列表）这个"假会话"就被覆盖掉，看起来像凭空消失。
   *
   * 后端不可用才回退到本地生成——属兜底，那种情况下刷新仍会丢。
   */
  const createNewSession = async () => {
    let sessionId = null;

    try {
      const response = await fetch(CREATE_SESSION_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: currentUser.value })
      });
      const data = await response.json();
      if (data.success && data.session_id) {
        sessionId = data.session_id;
      }
    } catch (error) {
      console.error('Error creating session:', error);
    }

    // 后端不可用时的兜底（刷新会丢，但当前会话仍可继续使用）
    if (!sessionId) {
      sessionId = `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    }

    const newSession = {
      session_id: sessionId,
      title: '新对话',
      create_time: formatTime(new Date()),
      memory: [],
      total_messages: 0
    };
    sessions.value.unshift(newSession);
    selectSession(newSession.session_id);
    return newSession;
  };

  const fetchSessions = async () => {
    if (!currentUser.value) return;

    isLoadingSessions.value = true;
    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: currentUser.value })
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      if (data.success && data.sessions) {
        sessions.value = data.sessions;
        // 默认选择最新的会话
        if (data.sessions.length > 0 && !selectedSessionId.value) {
          selectSession(data.sessions[0].session_id);
        }
      }
    } catch (error) {
      console.error('Error fetching sessions:', error);
    } finally {
      isLoadingSessions.value = false;
      onLoaded?.();
    }
  };

  /**
   * 加载指定会话的聊天记录（正文）。
   *
   * 会话列表接口只返回元信息（session_id / title / 条数），**不含正文**，
   * 所以点开某个会话时要用这个接口单独拉正文，避免列表请求把全部历史都拉回来。
   */
  const fetchSessionMessages = async (sessionId) => {
    if (!currentUser.value || !sessionId) return [];

    try {
      const response = await fetch(MESSAGES_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentUser.value,
          session_id: sessionId
        })
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      return data.success && data.messages ? data.messages : [];
    } catch (error) {
      console.error('Error fetching session messages:', error);
      return [];
    }
  };

  /**
   * 删除会话。
   *
   * 后端是**软删除**：调用后会话立刻从列表消失（毫秒级返回）；
   * 真实的清理（补归档 → 导出 → 真删 → 清 Redis）由后台协程异步完成，失败会自动重试。
   */
  const deleteSession = async (sessionId) => {
    if (!currentUser.value || !sessionId) return false;

    try {
      const response = await fetch(DELETE_SESSION_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentUser.value,
          session_id: sessionId
        })
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const data = await response.json();
      return !!data.success;
    } catch (error) {
      console.error('Error deleting session:', error);
      return false;
    }
  };

  const reset = () => {
    sessions.value = [];
    selectedSessionId.value = '';
  };

  return {
    sessions,
    selectedSessionId,
    isLoadingSessions,
    showSessions,
    toggleSessions,
    selectSession,
    createNewSession,
    fetchSessions,
    fetchSessionMessages,
    deleteSession,
    reset
  };
}
