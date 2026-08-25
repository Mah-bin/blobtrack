from setuptools import setup, find_packages

setup(
    name="blobtrack",
    version="0.1.0",
    description="Content-Aware Binary Version Control System",
    packages=find_packages(),
    python_requires=">=3.10",
    install_requires=[
        "fastcdc>=1.5.0",
        "zstandard>=0.21.0",
        "rich>=13.0.0",
    ],
    entry_points={
        "console_scripts": [
            "blobtrack=blobtrack.cli.main:main",
        ],
    },
)
