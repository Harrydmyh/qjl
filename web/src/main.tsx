import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

// 不用 StrictMode：开发模式下它会故意把 effect 跑两遍，
// 而这里的 effect 会去轮询/发请求，跑两遍容易误判成「任务重复启动」。
createRoot(document.getElementById('root')!).render(<App />);
