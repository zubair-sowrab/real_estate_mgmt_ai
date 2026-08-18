import os
from dotenv import load_dotenv

load_dotenv()

from langchain_groq import ChatGroq
from langgraph.prebuilt import create_react_agent

# Import the community web search tool
from langchain_community.tools import DuckDuckGoSearchRun

# Import our custom database tools
from .chatbot_tools import (
    get_current_tenant_info, get_contract_balance, get_unit_rent_history,
    get_portfolio_tenant_health_ranking, compare_portfolio_properties,
    get_cashflow_summary, get_portal_navigation_guide
)

# 1. Initialize Tools
web_search_tool = DuckDuckGoSearchRun(
    description="Searches the live internet (Google/DuckDuckGo). Use this ONLY when the user asks for general real estate advice, UAE laws, market trends, or information explicitly outside the Edrak database."
)

tools = [
    get_current_tenant_info, get_contract_balance, get_unit_rent_history,
    get_portfolio_tenant_health_ranking, compare_portfolio_properties,
    get_cashflow_summary, get_portal_navigation_guide, web_search_tool
]

# 2. Initialize LLM
api_key = os.getenv("GROQ_API_KEY")
if not api_key:
    raise ValueError("CRITICAL: GROQ_API_KEY is missing from environment variables.")

llm = ChatGroq(temperature=0.2, model_name="openai/gpt-oss-20b", api_key=api_key)

# 3. The Strict Business Advisor System Prompt
system_prompt = """
You are the elite Natural Language Database Assistant for the EDRAK Real Estate Portal. 
Your role is to act as a highly intelligent, accurate Property Manager and Financial Consultant.

STRICT BEHAVIORAL RULES:
1. ANTI-YAPPING PROTOCOL: The chat window is very small. KEEP IT SHORT. NEVER output large markdown tables, long navigational guides, or "What you can do next" sections unless explicitly requested. Maximum 4-5 short bullet points per answer.
2. NO CODE JARGON OR IDS: Never mention internal system identifiers like "Landlord ID", "Tenant ID", or "Node ID" to the user. Speak entirely in natural, professional language.
3. CURRENT CONTRACTS DEFAULT: Always assume the user wants information about CURRENT, ACTIVE leases and tenants unless they explicitly use words like "history," "past," or "previous."
4. FINANCIAL REPORTING: Always present tenant financials exactly as "Total Amount Debited", "Total Amount Credited", and "Pending PDCs (PDR)". 
5. IMPLIED PORTFOLIO SCOPE: If the user asks "compare my buildings" or "who is my worst tenant", DO NOT ask them to specify a building. Automatically use your portfolio-wide tools.
6. JUSTIFICATIONS: When explaining a tenant's grade, cite specific data briefly (e.g., "Paid 0% but 38% of lease elapsed"). Do not write long paragraphs.
7. WEB SEARCH: If the user asks for external information, use the DuckDuckGoSearchRun tool.

The system will secretly provide you with the user's `landlord_id` in the prompt. You MUST pass this exact integer to your tools silently. 
Format all currency safely as AED with commas (e.g., AED 5,000.00). Use Markdown (bolding, bullets) but NEVER TABLES.
"""

chatbot_agent = create_react_agent(
    model=llm,
    tools=tools,
    prompt=system_prompt
)