"""Sample Contoso feed for demo / screenshot mode (AVD_DEMO=1).

Anonymous, fixed data so documentation screenshots and the showcase video carry
no real account or organization details. Never used in normal operation.
"""


def demo_resources():
    """The fixed sample workspace list shown in demo mode."""
    def item(rid, title, kind, tenant):
        return {"id": rid, "title": title, "type": kind, "tenant": tenant, "icon32": None}
    return [
        item("d1", "build-box", "Desktop", "devcenter-contoso-dc / Engineering"),
        item("d2", "design-box", "Desktop", "devcenter-contoso-dc / Design"),
        item("d4", "Contoso Cloud PC", "Desktop", "Windows 365"),
        item("d5", "Finance Desktop - West Europe", "Desktop", "Contoso Virtual Desktops"),
        item("d6", "Finance Desktop - East US", "Desktop", "Contoso Virtual Desktops"),
        item("d3", "Word", "RemoteApp", "Productivity Apps - West Europe"),
        item("d7", "Excel", "RemoteApp", "Productivity Apps - West Europe"),
        item("d8", "Outlook", "RemoteApp", "Productivity Apps - West Europe"),
        item("d9", "Word", "RemoteApp", "Productivity Apps - East US"),
        item("d10", "Ops Console", "RemoteApp", "Contoso Tools"),
    ]
