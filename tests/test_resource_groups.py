from avd_feed_connect.gui import resource_groups as rg


def res(title, kind='Desktop', tenant='Workspace'):
    return {'id': title, 'title': title, 'type': kind, 'tenant': tenant}


FEED = [
    res('Word', 'RemoteApp', 'Productivity Apps - West Europe'),
    res('Europe Desktop', tenant='Microsoft Virtual Desktop - Cloud Regions'),
    res('juno', tenant='devcenter-abc-dc / MSTeams'),
    res('Word', 'RemoteApp', 'Productivity Apps - West US'),
    res('Laptop', tenant='Windows 365'),
]


def test_cloud_pcs_come_first_and_apps_group_by_workspace():
    sections = rg.sections(FEED)
    assert [kind for kind, _ in sections] == [rg.PC, rg.DESKTOP, rg.APP]
    assert [r['title'] for r in sections[0][1][0][1]] == ['juno', 'Laptop']
    assert [heading for heading, _ in sections[2][1]] == [
        'Productivity Apps - West Europe', 'Productivity Apps - West US']


def test_feed_workspace_name_wins_over_tenant():
    app = dict(res('Edge', 'RemoteApp', 'Contoso'), workspace='AVD-RemoteApps')
    assert rg.sections([app])[0][1][0][0] == 'AVD-RemoteApps'


def test_region_prefers_the_most_specific_name():
    assert rg.region(FEED[0]) == 'West Europe'
    assert rg.region(FEED[1]) == 'Europe'
    assert rg.region(res('West US 2 Desktop')) == 'West US 2'
    assert rg.region(FEED[2]) == ''


def test_filters_combine():
    assert [r['tenant'] for r in FEED if rg.matches(r, app_filter='Word', region_filter='United States')] == [
        'Productivity Apps - West US']
    assert [r['title'] for r in FEED if rg.matches(r, 'cloud regions')] == ['Europe Desktop']
    assert [r['title'] for r in FEED if rg.matches(r, kind_filter=rg.PC)] == ['juno', 'Laptop']
    options = rg.options(FEED)
    assert options['app'] == ['Word'] and options['kind'] == [rg.PC, rg.DESKTOP, rg.APP]


def test_region_filter_matches_the_whole_geography():
    feed = [res('Word', 'RemoteApp', 'Productivity Apps - Southeast Asia'),
            res('Asia Desktop', tenant='Microsoft Virtual Desktop - Legacy Regions'),
            res('Central India Desktop'), res('India Desktop')]
    assert rg.options(feed)['region'] == ['India', 'Asia Pacific']
    assert [r['title'] for r in feed if rg.matches(r, region_filter='Asia Pacific')] == ['Word', 'Asia Desktop']
    assert [r['title'] for r in feed if rg.matches(r, region_filter='India')] == [
        'Central India Desktop', 'India Desktop']


def test_options_follow_the_other_choices():
    options = rg.options(FEED, {'kind': rg.PC})
    assert options['region'] == [] and options['app'] == []
    assert options['workspace'] == ['devcenter-abc-dc / MSTeams', 'Windows 365']
    assert rg.options(FEED, {'region': 'Europe'})['kind'] == [rg.DESKTOP, rg.APP]
    assert rg.workspace_label('devcenter-abc-dc / MSTeams') == 'MSTeams'
