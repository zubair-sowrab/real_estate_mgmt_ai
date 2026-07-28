import requests
import xml.etree.ElementTree as ET

URL = "http://86.96.206.105:121/PactWebService.svc"
HEADERS = {'Content-Type': 'application/soap+xml; charset=utf-8'}


def explore_pact(query):
    xml_payload = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing"><s:Header><a:Action s:mustUnderstand="1">http://tempuri.org/IPactWebService/Get</a:Action><a:To s:mustUnderstand="1">{URL}</a:To></s:Header><s:Body><Get xmlns="http://tempuri.org/"><CompanyIndex>1</CompanyIndex><param xmlns:b="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:i="http://www.w3.org/2001/XMLSchema-instance"><b:anyType i:type="c:string" xmlns:c="http://www.w3.org/2001/XMLSchema"><![CDATA[{query}]]></b:anyType><b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">10048</b:anyType><b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">1</b:anyType></param><spName>spADM_GetDataSet</spName></Get></s:Body></s:Envelope>"""
    try:
        response = requests.post(URL, data=xml_payload.encode('utf-8'), headers=HEADERS, timeout=15)
        root = ET.fromstring(response.text)
        results = []
        for elem in root.iter():
            if elem.tag.split('}')[-1] == 'Table':
                results.append({col.tag.split('}')[-1]: col.text for col in elem})
        return results
    except Exception as e:
        return [{"Error": str(e)}]


print("🔎 X-RAY: HUNTING DOWN TRACKING NO '2950'...\n")

print("--- 1. WHERE IS '2950' HIDING IN ANITHA'S TCO VOUCHER? ---")
vouchers = explore_pact("SELECT * FROM ACC_DocDetails WITH(NOLOCK) WHERE VoucherNo = 'TCO-08/25/29'")
if vouchers and "Error" not in vouchers[0]:
    for v in vouchers:
        found_cols = [k for k, val in v.items() if str(val) == '2950']
        print(f"   ↳ Voucher {v.get('VoucherNo', '')} contains '2950' in columns: {found_cols}")
else:
    print(f"   ↳ {vouchers}")

print("\n--- 2. DOES HER CONTRACT STORE '2950'? ---")
contracts = explore_pact("SELECT * FROM REN_Contract WITH(NOLOCK) WHERE CCNodeID = 2950 OR CostCenterID = 2950")
if contracts and "Error" not in contracts[0]:
    for c in contracts:
        found_cols = [k for k, val in c.items() if str(val) == '2950']
        print(f"   ↳ Contract {c.get('ContractID', '')} contains '2950' in columns: {found_cols}")
else:
    print("   ↳ No contracts found with 2950 in standard CC columns.")

print("\n--- 3. LET'S CHECK IF 2950 IS ACTUALLY HER TENANT ID ---")
tenant = explore_pact("SELECT TenantID, FirstName, IDNumber FROM REN_Tenant WITH(NOLOCK) WHERE TenantID = 2950")
print(f"   ↳ {tenant}")