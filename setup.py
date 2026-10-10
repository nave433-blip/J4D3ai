from setuptools import setup, find_packages

setup(
    name="jade",
    version="0.3.0",
    packages=find_packages(exclude=["server", "server.*", "tests", "tests.*"]),
    py_modules=['cli'],
    python_requires=">=3.12",
    install_requires=[
        "typer>=0.25.1", "rich>=15.0.0", "requests>=2.34.2", "watchdog>=6.0.0",
        "sounddevice>=0.5.5", "scipy>=1.17.1", "numpy>=2.4.6",
        "faiss-cpu>=1.13.2",
        "SpeechRecognition>=3.16.1", "prompt_toolkit>=3.0.52", "dropbox>=12.0.2",
        "markdown>=3.10.2", "keyring>=25.7.0", "paramiko>=5.0.0",
        "psutil>=7.2.2", "ddgs>=9.14.4", "litellm>=1.85.1",
        "typing-extensions>=4.15.0", "packaging>=24.0", "urllib3>=2.8.0",
    ],
    entry_points={
        'console_scripts': [
            'jade=cli:app',
            'jade-thin=tools.ollama_thin:main',
            # Deprecated aliases: print a rename notice, then forward.
            'cortana=cli:cortana_shim',
            'cortana-thin=cli:cortana_thin_shim',
            'jarvis=cli:jarvis_shim',
        ],
    },
)
