import { ref, nextTick } from 'vue';

const API_URL = 'http://127.0.0.1:8000/api/query';

/**
 * 聊天消息 & SSE 流式响应处理
 * @param {object} options
 * @param {() => string|null} options.getUserId 取当前登录用户 ID
 * @param {() => string} options.getSessionId 取当前会话 ID
 * @param {() => void} [options.onUnauthorized] 未登录时的处理（通常是切回登录页）
 * @param {() => void} [options.onFinished] 一次请求结束后回调（通常是刷新会话列表）
 */
export function useChatStream({ getUserId, getSessionId, onUnauthorized, onFinished }) {
  const chatMessages = ref([]); // { type: 'user'|'assistant'|'THINKING', content: string, collapsed?: boolean }
  const userInput = ref('');
  const isProcessing = ref(false);
  let reader = null; // 保存读取器引用，用于取消请求

  const scrollToBottom = () => {
    setTimeout(() => {
      const chatContainer = document.querySelector('.chat-message-container');
      if (chatContainer) {
        chatContainer.scrollTop = chatContainer.scrollHeight;
      }
      // 确保页面不整体滚动
      window.scrollTo(0, 0);
    }, 0);
  };

  const toggleThinking = (index) => {
    const msg = chatMessages.value[index];
    if (msg && msg.type === 'THINKING') {
      msg.collapsed = !msg.collapsed;
    }
  };

  const reset = () => {
    chatMessages.value = [];
    userInput.value = '';
  };

  // 将后端返回的会话记忆填充到消息列表
  const loadFromMemory = (memory) => {
    let lastType = null;

    memory.forEach(msg => {
      if (!msg || !msg.content) return;

      let type = msg.role;
      if (type === 'process') type = 'THINKING';

      // 合并连续的思考过程
      if (type === 'THINKING' && lastType === 'THINKING') {
        const lastMsg = chatMessages.value[chatMessages.value.length - 1];
        lastMsg.content += '\n' + msg.content;
      } else {
        chatMessages.value.push({ type, content: msg.content });
      }
      lastType = type;
    });

    nextTick(scrollToBottom);
  };

  // 流式更新答案文本
  const streamTextToAnswer = (text) => {
    const lastMsg = chatMessages.value[chatMessages.value.length - 1];
    // 忽略打断思考过程的纯空白字符
    if ((!text || !text.trim()) && lastMsg && lastMsg.type !== 'assistant') {
      return;
    }

    text = text
      .replace(/ +/g, ' ')
      .replace(/\n+/g, '\n');

    if (lastMsg && lastMsg.type === 'assistant') {
      lastMsg.content += text;
    } else {
      chatMessages.value.push({ type: 'assistant', content: text });
    }
    chatMessages.value = [...chatMessages.value]; // Trigger reactivity

    scrollToBottom();
  };

  // 流式更新思考 / 流程消息
  const streamTextToProcess = (text) => {
    const lastMsg = chatMessages.value[chatMessages.value.length - 1];
    if (lastMsg && lastMsg.type === 'THINKING') {
      lastMsg.content += text;
      if (isProcessing.value && lastMsg.collapsed === undefined) {
        lastMsg.collapsed = false;
      }
    } else {
      chatMessages.value.push({ type: 'THINKING', content: text, collapsed: false });
    }
    chatMessages.value = [...chatMessages.value];

    scrollToBottom();
  };

  // 处理 SSE 格式的数据
  const processSSEData = (data) => {
    try {
      if (typeof data !== 'string') return;

      if (!data.startsWith('data:')) return;

      const jsonStr = data.substring(5).trim();
      if (!jsonStr) return;

      let kind;
      let text;

      try {
        const parsedData = JSON.parse(jsonStr);

        if (parsedData.content && typeof parsedData.content === 'object') {
          // 新版 StreamPacket: { content: { kind: '...', text: '...', ... }, ... }
          text = parsedData.content.text;

          if (parsedData.content.kind) {
            kind = parsedData.content.kind;
          } else if (parsedData.content.type) {
            // 兼容旧版字段名
            kind = parsedData.content.type;
          }

          // 结束信号
          if (parsedData.status === 'FINISHED' || parsedData.content.contentType === 'sagegpt/finish') {
            return;
          }
        } else if (parsedData.type && parsedData.content) {
          // 降级兼容旧格式
          kind = parsedData.type;
          text = parsedData.content;
        }
      } catch (jsonError) {
        console.error('JSON parse error:', jsonError);
        return;
      }

      if (!kind || !text) return;

      switch (kind) {
        case 'ANSWER':
          streamTextToAnswer(text);
          break;
        case 'THINKING':
          streamTextToProcess(text);
          break;
        case 'PROCESS':
          streamTextToProcess(text + '\n');
          scrollToBottom();
          break;
        default:
          console.log('Unknown content kind:', kind);
          streamTextToProcess(text + '\n');
      }
    } catch (error) {
      console.error('Error processing SSE data:', error);
    }
  };

  const handleSend = async () => {
    if (!userInput.value.trim()) return;

    // 立即强制滚动到页面顶部，防止页面下移
    window.scrollTo(0, 0);

    // 发送时才校验登录态
    const userId = getUserId();
    if (!userId) {
      onUnauthorized?.();
      return;
    }

    isProcessing.value = true;

    // 自动收起之前的思考过程
    chatMessages.value.forEach(msg => {
      if (msg.type === 'THINKING') {
        msg.collapsed = true;
      }
    });

    chatMessages.value.push({
      type: 'user',
      content: userInput.value.trim()
    });

    const requestData = {
      query: userInput.value.trim(),
      context: {
        user_id: userId,
        session_id: getSessionId() || ''
      }
    };

    console.log('发送请求，会话ID:', getSessionId());
    console.log('发送请求，用户ID:', userId);

    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestData)
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          if (buffer.trim()) {
            processSSEData(buffer);
            buffer = '';
          }
          break;
        }

        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;

        const lines = buffer.split('\n');
        // 除了最后一行（可能不完整）外，处理所有行
        for (let i = 0; i < lines.length - 1; i++) {
          if (lines[i].trim()) {
            processSSEData(lines[i]);
          }
        }
        buffer = lines[lines.length - 1];
      }
    } catch (error) {
      if (!error.name || error.name !== 'AbortError') {
        streamTextToProcess(`请求失败: ${error.message}\n`);
        console.error('Error:', error);
      }
    } finally {
      isProcessing.value = false;
      reader = null;
      scrollToBottom();
      onFinished?.();
    }

    userInput.value = '';
  };

  const handleCancel = () => {
    if (reader) {
      reader.cancel();
      reader = null;
    }
    isProcessing.value = false;
    streamTextToProcess('请求已取消\n');
  };

  return {
    chatMessages,
    userInput,
    isProcessing,
    scrollToBottom,
    toggleThinking,
    reset,
    loadFromMemory,
    handleSend,
    handleCancel
  };
}
