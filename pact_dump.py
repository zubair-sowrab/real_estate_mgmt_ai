import requests
import xml.etree.ElementTree as ET
import csv
import os

URL = "http://86.96.206.105:121/PactWebService.svc"
HEADERS = {
    'Content-Type': 'application/soap+xml; charset=utf-8',
    'Accept-Encoding': 'gzip, deflate'
}


# The exact fetch function from your pact_explorer.py
def fetch_raw_pact_data(sql_query):
    xml_payload = f"""<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="http://www.w3.org/2005/08/addressing">
    <s:Header>
        <a:Action s:mustUnderstand="1">http://tempuri.org/IPactWebService/Get</a:Action>
        <a:To s:mustUnderstand="1">http://86.96.206.105:121/PactWebService.svc</a:To>
    </s:Header>
    <s:Body>
        <Get xmlns="http://tempuri.org/">
            <CompanyIndex>1</CompanyIndex>
            <param xmlns:b="http://schemas.microsoft.com/2003/10/Serialization/Arrays" xmlns:i="http://www.w3.org/2001/XMLSchema-instance">
                <b:anyType i:type="c:string" xmlns:c="http://www.w3.org/2001/XMLSchema"><![CDATA[{sql_query}]]></b:anyType>
                <b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">10048</b:anyType>
                <b:anyType i:type="c:int" xmlns:c="http://www.w3.org/2001/XMLSchema">1</b:anyType>
            </param>
            <spName>spADM_GetDataSet</spName>
        </Get>
    </s:Body>
</s:Envelope>"""

    response = requests.post(URL, data=xml_payload.encode('utf-8'), headers=HEADERS)

    if response.status_code == 200:
        root = ET.fromstring(response.text)
        records = []
        for elem in root.iter():
            if elem.tag.split('}')[-1] == 'Table':
                row_data = {}
                for column in elem:
                    col_name = column.tag.split('}')[-1]
                    row_data[col_name] = column.text or ""
                records.append(row_data)
        return records
    else:
        return []


print("🔌 Connecting to PACT API...")

dump_folder = "PACT_Database_Dump"
os.makedirs(dump_folder, exist_ok=True)

# Fetch the names of every single table in the database
print("📋 Fetching table list...")
tables_data = fetch_raw_pact_data("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
tables = [row['TABLE_NAME'] for row in tables_data if 'TABLE_NAME' in row]

# 💡 OPTIONAL: If you only want to download the Real Estate tables, uncomment the next line:
# tables = [t for t in tables if t.startswith('REN_')]

print(f"Found {len(tables)} tables! Starting extraction...\n")

for table in tables:
    print(f"📥 Exporting {table}...")
    try:
        # Pull everything from the table
        table_data = fetch_raw_pact_data(f"SELECT * FROM {table} WITH (NOLOCK)")

        if not table_data:
            print(f"   ↳ ⚠️ No data found (Empty table). Skipped.")
            continue

        # Get the column names for the CSV header from the first row
        columns = list(table_data[0].keys())

        # Write to a CSV file named after the table
        with open(f"{dump_folder}/{table}.csv", "w", newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(columns)

            # Write all rows
            for row in table_data:
                cleaned_row = [str(row.get(col, '')).strip() for col in columns]
                writer.writerow(cleaned_row)

        print(f"   ↳ ✅ Saved {len(table_data)} rows.")

    except Exception as e:
        print(f"   ↳ ❌ Error: {e}")

print("\n✅ Extraction Complete! Check the 'PACT_Database_Dump' folder.")