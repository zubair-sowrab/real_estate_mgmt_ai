import os
from dotenv import load_dotenv

load_dotenv()

from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent

# Import ALL 15 tools we built
from .chatbot_tools import (
    get_vacant_units, get_tenant_contact_info, get_contract_balance,
    get_rent_growth_report, compare_properties, get_tenant_performance,
    get_maintenance_expenses, get_cashflow_summary, get_portal_navigation_guide,
    get_unit_rent_history, get_bounced_cheques_report, get_expiring_contracts,
    get_pending_approvals, search_documents, get_last_sync_status
)

# 1. Define the Tools List
tools = [
    get_vacant_units, get_tenant_contact_info, get_contract_balance,
    get_rent_growth_report, compare_properties, get_tenant_performance,
    get_maintenance_expenses, get_cashflow_summary, get_portal_navigation_guide,
    get_unit_rent_history, get_bounced_cheques_report, get_expiring_contracts,
    get_pending_approvals, search_documents, get_last_sync_status
]


# 2. Initialize the Groq LLM
llm = ChatGroq(temperature=0, model_name="openai/gpt-oss-120b")

# 3. Define the System Prompt
system_prompt = """
You are the elite Natural Language Database Assistant for the YBN Real Estate Portal.
Your job is to answer the landlord's questions accurately using the provided tools.

CRITICAL RULES:
1. You are bilingual. Seamlessly understand and reply in plain English or Arabic, depending on the language the user used in their prompt.
2. The system will secretly provide you with the user's `landlord_id` in the prompt. You MUST pass this exact integer to your tools. Never ask the user for their ID.
3. Format all currency as AED with commas (e.g., AED 5,000.00).
4. If a tool returns no data, politely inform the user rather than guessing or hallucinating answers.
5. Use markdown formatting (bullet points, bold text) to make financial breakdowns easy to read.
"""

# 4. Create the LangGraph Agent
chatbot_agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=system_prompt
)