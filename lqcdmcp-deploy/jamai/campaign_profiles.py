"""Declarative current-campaign overlays; historical catalog documents stay exact."""
from copy import deepcopy


def project(source, profile):
    if profile.get('schema') != 'femtomind.campaign_defaults.v1' or profile.get('historical_analysis_id') != source['analysis_id']:
        raise ValueError('Campaign defaults must identify their historical catalog')
    result = deepcopy(source)
    result['analysis_id'] = profile['analysis_id']
    result['scope']['target'] = profile['description']
    result['scope']['campaign_defaults'] = deepcopy(profile)
    result['scope']['historical_statistics_notice'] = 'Published statistics and fit results describe the historical reference, not this updated, unfitted configuration.'
    result['sources']['campaign_defaults'] = {'source': 'campaign_defaults.json', 'authority': 'User-confirmed updated campaign requirements; not a historical publication correction'}
    sv = result['source_verification']
    for reaction, substitutions in profile['dataset_substitutions'].items():
        for row in sv['datasets'][reaction]['entries']:
            old = row['id']
            if old in substitutions:
                row['id'] = substitutions[old]
                row['xlsx_path'] = reaction + '/expdata/' + row['id'] + '.xlsx'
                row['reference'] = 'campaign_defaults.json; replaces historical ' + reaction + ':' + old
                row['evidence_origin'] = 'updated_campaign_defaults_not_historical_input'
                row['current_reader_verification'] = deepcopy(profile['reader_evidence'][reaction][row['id']])
    for reaction, filters in profile['reaction_filters'].items():
        block = sv['datasets'][reaction]
        block['raw_filter_configuration'] = deepcopy(filters)
        block['filters_key_present'] = True
        for row in block['entries']:
            row['filters'] = deepcopy(filters.get(row['id'], []) if isinstance(filters, dict) else filters)
            row['filters_key_present'] = not isinstance(filters, dict) or row['id'] in filters
    sv['global_input_settings'] = [row for row in sv['global_input_settings'] if row['key'] not in profile['ignored_native_settings']]
    for row in sv['global_input_settings']:
        if row['key'] in profile['native_settings']:
            row['value'] = profile['native_settings'][row['key']]
    present = {row['key'] for row in sv['global_input_settings']}
    for key, value in profile['native_settings'].items():
        if key not in present:
            sv['global_input_settings'].append({'key': key, 'value': value, 'reference': 'campaign_defaults.json'})
    sv['current_reader_point_validation'] = {
        'method': 'Existing native readers in the JLab scientific container; no fitting or sampling',
        'artifact_sha256': profile['reader_validation_sha256'],
        'summary': deepcopy(profile['expected_accepted_points']),
        'warnings': [profile['boundary_notice']],
        'scope': 'Updated current-campaign workbooks; not historical ensemble identity evidence'}
    sv.setdefault('latest_author_corrections', {})['updated_campaign_defaults'] = {
        'profile': profile['analysis_id'], 'historical_total_is_not_a_parity_target': True,
        'current_accepted_points': profile['expected_accepted_points'],
        'PDF_Gaussian_metadata': 'Optional in the ordinary campaign workflow; FF Gaussian initialization remains required.'}
    return result
