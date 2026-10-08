"""Airport-local time zones for display, without changing stored timestamps."""
from .providers import _airport_tz


def with_flight_timezones(record):
    if record.get('mode') != 'flight':
        return record
    metadata = record.get('metadata') or {}
    raw = metadata.get('flight')
    if not isinstance(raw, dict):
        return record
    info = dict(raw)
    summary = info.get('summary') if isinstance(info.get('summary'), dict) else {}
    departure = (info.get('departure_airport') or info.get('origin') or record.get('origin')
                 or summary.get('orig_icao') or summary.get('orig_iata'))
    arrival = (info.get('arrival_airport_actual') or summary.get('dest_icao_actual')
               or info.get('arrival_airport') or info.get('destination') or record.get('destination')
               or summary.get('dest_icao') or summary.get('dest_iata'))
    for field, airport in (('departure_timezone', departure), ('arrival_timezone', arrival)):
        zone = _airport_tz(airport)
        info[field] = zone.key if zone else None
    return {**record, 'metadata': {**metadata, 'flight': info}}
