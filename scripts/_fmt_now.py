import sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
from map_sheets import format_po_tab
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
print('formatted sheetId:', format_po_tab(sid))
