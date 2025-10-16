const express = require('express');
const app = express();
app.get('/health', (_req, res) => res.json({ status: 'ok' }));
app.get('/', (_req, res) => res.send('Hello from Free Tier POC!'));
const port = process.env.PORT || 3000;
if (require.main === module) app.listen(port, () => console.log(`App on ${port}`));
module.exports = app;
