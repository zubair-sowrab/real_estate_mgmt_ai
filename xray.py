import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')
django.setup()

from pact_sync import fetch_from_pact


def debug_job():
    print("\n🔍 --- HUNTING FOR THE JOB'S TRUE UNIT (NODE 1491) ---")

    # 41075 was the CostCenterID for the PIC-01/26/1536 and 1530 vouchers.
    # Let's check exactly what this node is.
    q_cc = """
        SELECT NodeID, Name, Code 
        FROM COM_CostCenter WITH(NOLOCK) 
        WHERE NodeID = 41075
    """
    print("\n📂 1. Cost Center Master Data:")
    for r in fetch_from_pact(q_cc) or []: print(r)

    # Let's check the hidden dimensions attached to the Job itself!
    q_dim = """
        SELECT NodeID, dcCCNID9 AS PropertyNode, dcCCNID10 AS UnitNode 
        FROM COM_CCData WITH(NOLOCK) 
        WHERE NodeID = 41075
    """
    print("\n📂 2. Cost Center Hidden Dimensions:")
    for r in fetch_from_pact(q_dim) or []:
        clean = {k: v for k, v in r.items() if str(v).strip() not in ['0', '', 'None']}
        if clean: print(clean)

    # Let's explicitly check what Job ID belongs to 1538
    q_1538 = """
        SELECT VoucherNo, CostCenterID 
        FROM ACC_DocDetails WITH(NOLOCK) 
        WHERE VoucherNo = 'PIC-01/26/1538' AND Amount = 60
    """
    print("\n📂 3. Exact CostCenterID for PIC-01/26/1538:")
    target_cc = None
    for r in fetch_from_pact(q_1538) or []:
        print(r)
        target_cc = r.get('costcenterid')

    if target_cc and str(target_cc) not in ['0', 'None']:
        q_target = f"""
            SELECT NodeID, dcCCNID9 AS PropertyNode, dcCCNID10 AS UnitNode 
            FROM COM_CCData WITH(NOLOCK) 
            WHERE NodeID = {target_cc}
        """
        print(f"\n📂 4. Dimensions for Target Job {target_cc}:")
        for r in fetch_from_pact(q_target) or []:
            clean = {k: v for k, v in r.items() if str(v).strip() not in ['0', '', 'None']}
            if clean: print(clean)


if __name__ == "__main__":
    debug_job()