"""SDK quickstart — requires a running API."""

from dataagent_sdk import Client

client = Client(base_url="http://127.0.0.1:8000", principal="demo-admin")
project_id = "REPLACE"
print(client.agents.list(project_id))
tools = client.as_tools(project_id)
print([t["name"] for t in tools])
