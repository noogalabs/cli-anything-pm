import shutil
from pathlib import Path

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py


class CleanBuildPy(build_py):
    """Prevent a prior build's modules from leaking into a new wheel."""

    def run(self):
        build_lib = Path(self.build_lib)
        if build_lib.exists():
            shutil.rmtree(build_lib)
        super().run()


setup(
    name="cli-anything-pm",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    package_data={"cli_anything.propertymeld.recapture": ["*.py"]},
    cmdclass={"build_py": CleanBuildPy},
    install_requires=[
        "click>=8.0",
        "playwright>=1.40",
        "requests>=2.31",
        "pyarrow>=25.0,<26",
    ],
    entry_points={
        "console_scripts": ["pm=cli_anything.propertymeld.cli:cli"],
        "snapcli.platforms": ["pm=cli_anything.propertymeld.cli:cli"],
    },
)
