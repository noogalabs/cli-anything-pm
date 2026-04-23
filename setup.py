from setuptools import setup, find_packages

setup(
    name="cli-anything-propertymeld",
    version="0.1.0",
    packages=find_packages(),
    install_requires=["click>=8.0", "playwright>=1.40", "requests>=2.31"],
    entry_points={
        "console_scripts": ["pm=cli_anything.propertymeld.cli:cli"],
    },
)
