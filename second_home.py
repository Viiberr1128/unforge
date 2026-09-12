"""Same-Mac stand-in for a second computer.

The iOS Simulator cannot run Unforge.app (it is a Mac app). This opens a
recovered capsule in a brand-new workspace folder on this Mac, the same way
File → Open Workspace would on another machine.
"""
from pathlib import Path

from engine import Engine, Problem
from recovery import Recovery


class SecondHome:
    def __init__(self, engine):
        self.engine = engine

    def explain(self):
        return {
            'simulatorUsable': False,
            'reason': 'Unforge is a Mac app. The iOS Simulator cannot open it. Recover into a new folder on this Mac, then File → Open Workspace.',
            'sameMacStandIn': True,
        }

    def from_capsule(self, capsule_path, destination):
        if not isinstance(destination, str) or not Path(destination).is_absolute():
            raise Problem('Choose an absolute empty folder for the second workspace')
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise Problem('Restore into a new folder; existing work is never overwritten.')
        if target.resolve().is_relative_to(self.engine.home.resolve()) or self.engine.home.resolve().is_relative_to(target.resolve()):
            raise Problem('The second workspace must sit outside your current Unforge home')
        target.parent.mkdir(parents=True, exist_ok=True)
        other = Engine(target)
        imported = Recovery(other).import_capsule(capsule_path)
        inventory = other.project_inventory()
        return {
            'home': str(target),
            'project': imported,
            'projects': inventory.get('projects') if isinstance(inventory, dict) else inventory,
            'applicationsStarted': False,
            'simulatorUsable': False,
            'sameMacStandIn': True,
        }
