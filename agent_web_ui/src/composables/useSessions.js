import { ref } from 'vue';

const API_URL = 'http://127.0.0.1:8000/api/user_sessions';

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

  const createNewSession = () => {
    const newSession = {
      session_id: `session_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`,
      create_time: new Date().toISOString(),
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
    reset
  };
}
