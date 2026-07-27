import requests
import xml.etree.ElementTree as ET

# Pointing to the live server
URL = "http://86.96.206.105:121/PactWebService.svc"
COMPANY_INDEX = "1"

# We grab the top 20 most recently created BRVs based on database ID
sql_query = """
SELECT TOP 20 
    AccDocDetailsID, VoucherNo, DocDate, Amount, StatusID, CommonNarration
FROM ACC_DocDetails WITH (NOLOCK) 
WHERE DocAbbr = 'BRV' 
ORDER BY AccDocDetailsID DESC
"""

xml_payload = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://tempuri.org/IPactWebService/Get</a:Action>
        <a:To s:mustUnderstand="1">{URL}</a:To>
    </s:Header>
    <s:Body>
        <Get xmlns="http://tempuri.org/">
            <CompanyIndex>{COMPANY_INDEX}</CompanyIndex>
            <param xmlns:b="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
                <b:anyType i:type="c:string" xmlns:c="http://www.w3.org/2001/XMLSchema"><![CDATA[{sql_query}]]></b:anyType>
                <b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">10048</b:anyType>
                <b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">1</b:anyType>
            </param>
            <spName>spADM_GetDataSet</spName>
        </Get>
    </s:Body>
</s:Envelope>"""

headers = {'Content-Type': 'application/soap+xml; charset=utf-8'}

try:
    print("📡 Fetching the 20 most recently created BRVs directly from PACT...\n")
    response = requests.post(URL, data=xml_payload.encode('utf-8'), headers=headers, timeout=15)

    if response.status_code == 200:
        root = ET.fromstring(response.text)

        print(f"{'-' * 105}")
        print(
            f"{'Voucher No':<20} | {'DocDate':<10} | {'Amount':<10} | {'StatusID':<10} | {'AccDocID':<10} | {'Narration'}")
        print(f"{'-' * 105}")

        for elem in root.iter():
            if elem.tag.split('}')[-1] == 'Table':
                row = {col.tag.split('}')[-1]: col.text for col in elem}

                v_no = str(row.get('VoucherNo', 'N/A'))[:19]
                d_date = str(row.get('DocDate', 'N/A'))
                amt = str(row.get('Amount', 'N/A'))
                stat = str(row.get('StatusID', 'N/A'))
                doc_id = str(row.get('AccDocDetailsID', 'N/A'))
                narr = str(row.get('CommonNarration', ''))[:30]  # Trim narration to keep the table clean

                print(f"{v_no:<20} | {d_date:<10} | {amt:<10} | {stat:<10} | {doc_id:<10} | {narr}")

        print(f"{'-' * 105}")
    else:
        print(f"❌ Server returned HTTP {response.status_code}")
except Exception as e:
    print(f"❌ Connection Error: {e}")