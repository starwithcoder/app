import { ref } from 'vue';

const STORAGE_KEY = 'currentUserId';

// 测试用户列表（对接真实后端后应删除，改由服务端校验）
const VALID_USERS = [
  { username: 'root1', password: '123456', userId: 'root1' },
  { username: 'root2', password: '123456', userId: 'root2' },
  { username: 'root3', password: '123456', userId: 'root3' }
];

export function useAuth() {
  // TODO: 后端接口就绪后改为 !!localStorage.getItem(STORAGE_KEY)
  const isLoggedIn = ref(true);
  const username = ref('');
  const password = ref('');
  const currentUser = ref('');
  const loginError = ref('');

  // 从 localStorage 恢复当前用户
  const restoreUser = () => {
    const savedUserId = localStorage.getItem(STORAGE_KEY);
    const savedUser = VALID_USERS.find(u => u.userId === savedUserId);
    if (savedUser) {
      currentUser.value = savedUser.username;
    }
  };
  restoreUser();

  const handleLogin = () => {
    loginError.value = '';
    const user = VALID_USERS.find(
      u => u.username === username.value && u.password === password.value
    );

    if (!user) {
      loginError.value = '用户名或密码错误';
      return false;
    }

    isLoggedIn.value = true;
    currentUser.value = user.username;
    localStorage.setItem(STORAGE_KEY, user.userId);
    window.scrollTo(0, 0);
    username.value = '';
    password.value = '';
    return true;
  };

  // 清除登录态（登出 / 回到登录页共用）
  const clearLoginState = () => {
    isLoggedIn.value = false;
    currentUser.value = '';
    localStorage.removeItem(STORAGE_KEY);
  };

  const getUserId = () => localStorage.getItem(STORAGE_KEY);

  return {
    isLoggedIn,
    username,
    password,
    currentUser,
    loginError,
    handleLogin,
    logout: clearLoginState,
    goToLogin: clearLoginState,
    getUserId
  };
}
