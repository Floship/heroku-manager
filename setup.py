from setuptools import setup, find_packages

setup(
    name="heroku_manager",
    version="0.1.2",
    packages=find_packages(),
    install_requires=[
        "django>=2.2",
        "requests>=2.0.0",
        "tenacity>=6.0.0",
    ],
    author="Floship",
    author_email="stas@floship.com",
    description="A package for managing Heroku dynos with autoscaling capabilities",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://github.com/floship/heroku-manager",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.6",
)
