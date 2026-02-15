"""
Setup configuration for PyDownLib package
"""

from setuptools import setup, find_packages

setup(
    name="pydownlib",
    version="0.1.0",
    description="Simple asynchronous download manager",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="PyDownLib Contributors",
    url="https://github.com/nbpm128/PyDownLib",
    project_urls={
        "Documentation": "https://github.com/nbpm128/PyDownLib#readme",
        "Repository": "https://github.com/nbpm128/PyDownLib.git",
    },
    packages=find_packages(),
    python_requires=">=3.12",
    install_requires=[
        "aiofiles>=25.1.0",
        "httpx>=0.28.1",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0",
            "pytest-asyncio>=0.21.0",
            "black>=23.0",
            "flake8>=6.0",
            "mypy>=1.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "pydownlib=pydownlib.cli:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Programming Language :: Python :: 3.14",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Internet :: WWW/HTTP",
        "Topic :: System :: Archiving :: Mirroring",
    ],
    keywords="download async asyncio manager resume hash-verification",
    license="MIT",
)
