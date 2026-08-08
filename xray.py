import os
import django
import requests
import xml.etree.ElementTree as ET
import json
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

URL = os.getenv("PACT_URL", "http://127.0.0.1:121/PactWebService.svc")
COMPANY_INDEX = os.getenv("PACT_COMPANY_INDEX", "1")


def fetch_from_pact(sql_query):
    xml_payload = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing"><s:Header><a:Action s:mustUnderstand="1">http://tempuri.org/IPactWebService/Get</a:Action><a:To s:mustUnderstand="1">{URL}</a:To></s:Header><s:Body><Get xmlns="http://tempuri.org/"><CompanyIndex>{COMPANY_INDEX}</CompanyIndex><param xmlns:b="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><b:anyType i:type="c:string" xmlns:c="http://www.w3.org/2001/XMLSchema"><![CDATA[{sql_query}]]></b:anyType><b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">10048</b:anyType><b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">1</b:anyType></param><spName>spADM_GetDataSet</spName></Get></s:Body></s:Envelope>"""
    headers = {'Content-Type': 'application/soap+xml; charset=utf-8'}
    response = requests.post(URL, data=xml_payload.encode('utf-8'), headers=headers)

    records = []
    if response.status_code == 200:
        root = ET.fromstring(response.text)
        for elem in root.iter():
            if elem.tag.split('}')[-1] == 'Table':
                row_data = {}
                for column in elem:
                    col_name = column.tag.split('}')[-1]
                    row_data[col_name] = column.text or ""
                records.append(row_data)
    return records


print("🔍 X-Raying PACT Database for JRNs...\n")

query = """
SELECT 
    d.VoucherNo, d.Amount, d.CostCenterID, d.RefCCID, 
    d.CommonNarration, d.LineNarration, 
    (SELECT TOP 1 AccountName FROM ACC_Accounts WITH(NOLOCK) WHERE AccountID = d.DebitAccount) as DebitName,
    (SELECT TOP 1 AccountName FROM ACC_Accounts WITH(NOLOCK) WHERE AccountID = d.CreditAccount) as CreditName,
    (SELECT TOP 1 ccd.dcCCNID19 FROM COM_DocCCData ccd WITH(NOLOCK) WHERE ccd.AccDocDetailsID = d.AccDocDetailsID) as HiddenContractNode, 
    (SELECT TOP 1 ccd.dcCCNID11 FROM COM_DocCCData ccd WITH(NOLOCK) WHERE ccd.AccDocDetailsID = d.AccDocDetailsID) as TrueTenantNode 
FROM ACC_DocDetails d WITH(NOLOCK) 
WHERE d.VoucherNo IN ('JRN-02/26/21', 'JRN-02/25/92', 'JRN-08/26/15')
"""

results = fetch_from_pact(query)

for r in results:
    print(json.dumps(r, indent=4))
    print("-" * 50)