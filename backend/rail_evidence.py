"""Read GPS uncertainty for railway inference without changing stored CSV/payload."""
import csv
import math


def load_rail_trip(store,trip_id):
    trip=store.get(trip_id)
    if trip is None:return None
    with store.connect() as db:
        for row in db.execute('SELECT sequence,raw FROM point_csv WHERE trip_id=?',(trip_id,)):
            fields=next(csv.reader([row['raw']]))
            if len(fields)!=11:continue
            try:accuracy=float(fields[5])
            except ValueError:continue
            if math.isfinite(accuracy) and accuracy>=0 and 0<=row['sequence']<len(trip['points']):
                trip['points'][row['sequence']]['_accuracy_m']=accuracy
    return trip
