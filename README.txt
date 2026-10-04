这个目录放的是「旧版单文件前端」，只含页面，不含后端代码和任何数据。

· index.html —— 和项目根目录同一份，改完前端记得同步一次：
      cd .. && cp index.html deploy/index.html
  （页面里没有任何密钥；后端地址和访问密钥是打开时用 ?api=&k= 传进去、存在浏览器本地的）

· 部署：在本目录里执行
      npx vercel deploy --prod
  或者把 deploy 目录直接拖进 vercel.com 的 New Project。

· 打开方式（把 <前端地址> 换成部署后拿到的地址）：
      <前端地址>/?api=https://<你的后端地址>&k=<访问密钥>
  带一次就会记住，以后直接开 <前端地址> 即可。

· 注意：Vercel 上要连的后端必须是 HTTPS —— Vercel 页面是 https，
  后端是 http 会被浏览器当「混合内容」拦掉。

──────────────────────────────────────────────
新版组件化前端在 ../web/（Vite + React + TypeScript），推荐用它：
      cd ../web && npm install && npm run build     # 产物在 web/dist
  Vercel 导入仓库时把 Root Directory 设成 web。
