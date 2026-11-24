# dataagent-sdk

Independently installable Python SDK for Data Agent.

```bash
pip install dataagent-sdk
```

Depends only on `httpx` and `pydantic` — no server packages required.

```python
from dataagent_sdk import Client

client = Client(base_url="http://localhost:8000", token="...")
client.events.batch(project_id, events)
client.analytics.charts(project_id, metric="session_volume")
tools = client.as_tools()  # JSON Schema + handlers
```
